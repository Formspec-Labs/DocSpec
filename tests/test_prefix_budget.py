"""Saved prefix reuse preserves work limits across worker restarts."""

from dataclasses import fields, replace

import pytest

from docspec.application.work_budget import WorkBudget
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import identity_digest, sha256_digest
from docspec.domain.jobs import EntryExecutionMode, StoreState
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorSet
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.extraction import ExtractionResult
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
from docspec.workspace import LocalWorkspace
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.support.processors import _CountingExtractor, _CountingProcessor, _CountingSegmenter, _description
from tests.support.profiles import _seeded_local_run_arguments
from tests.support.representation import FAKE_PDF_PAGES, FIXTURES, install_fake_pypdf


def _replan(plan, **changes):
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


@pytest.fixture
def pdf_run(tmp_path, monkeypatch):
    install_fake_pypdf(monkeypatch)
    arguments = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    workspace = arguments["workspace"]
    content, _ = FIXTURES["application/pdf"]
    (workspace.roots["sourceContent"] / "document.pdf").write_bytes(content)
    source_root = tmp_path / "pdf-source-catalog"
    source = write_shared_source_catalog(source_root, (SourceItem(
        "pdf-document", "1", (CandidateFile(
            "primary", "document.pdf", "application/pdf",
            expected_digest=sha256_digest(content), expected_size=len(content),
        ),), metadata={"expectedPages": len(FAKE_PDF_PAGES), "expectedSegments": len(FAKE_PDF_PAGES)},
    ),))
    arguments["workspace"] = LocalWorkspace(workspace.root, workspace.roots | {"sourceCatalog": source_root})
    arguments["plan"] = _replan(arguments["plan"], source_catalog=source, processors=ProcessorSet(()), stages=stage_policy())
    fetcher = SharedFixtureContentFetcher(workspace.roots["sourceContent"])
    fetches = []
    fetch = fetcher.fetch

    def observed(candidate, **kwargs):
        fetches.append(candidate.candidate_id)
        return fetch(candidate, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", observed)
    arguments["content_fetcher"] = fetcher
    return arguments, fetches, _CountingExtractor(), _CountingSegmenter(), content


def _interrupt(prepared, monkeypatch, predicate):
    """Stop only after the executor has saved and verified the chosen frontier."""
    class WorkerStopped(BaseException):
        pass

    task = next(prepared.task_source(prepared.handoff))
    stores = prepared._composition.stores
    save = stores.save

    def interrupt_after_save(store):
        reference = save(store)
        if store.state is StoreState.RUNNING and not store.entries[0].terminal and predicate(store.entries[0]):
            raise WorkerStopped()
        return reference

    with monkeypatch.context() as isolated:
        isolated.setattr(stores, "save", interrupt_after_save)
        with pytest.raises(WorkerStopped):
            prepared.execute_task(prepared.handoff, task)
    latest = stores.latest(task.input_store.store_id)
    assert latest is not None
    entry = stores.load(latest).entries[0]
    checkpoint = prepared._composition.executor._checkpoints.verify_entry(entry, prepared._composition.plan)
    return task, entry, checkpoint


def _restored_budget(entry, checkpoint, limits):
    budget = WorkBudget(limits)
    invocations = {entry.entry_id: checkpoint.processor_invocations}
    observations = {entry.entry_id: checkpoint.extraction_observations}
    budget.seed_verified_entries((entry,), invocations, observations)
    first = budget.usage
    budget.seed_verified_entries((entry,), invocations, observations)
    assert budget.usage == first
    return budget


class _ObservedWorkExtractor:
    """Report inspected work separately from the retained representation shape."""

    extractor_id = "tests.observed-prefix-extractor/v1"

    def __init__(self, delegate, observation_field, count):
        self.delegate = delegate
        self.observation_field = observation_field
        self.count = count

    @property
    def configuration_digest(self):
        return identity_digest({
            "delegate": self.delegate.configuration_digest,
            "observationField": self.observation_field,
            "count": self.count,
        })

    def selected_identity(self, captured):
        return self.delegate.selected_identity(captured)

    def extract(self, captured, content):
        result = self.delegate.extract(captured, content)
        metadata = {key: value for key, value in result.receipt.metadata.items() if key not in {"pageCount", "frameCount"}}
        return ExtractionResult(result.payload, replace(result.receipt, metadata=metadata | {self.observation_field: self.count}))


@pytest.mark.parametrize("mode", [EntryExecutionMode.FULL, EntryExecutionMode.FROM_CAPTURES])
@pytest.mark.parametrize("observation_field", ["pageCount", "frameCount"])
def test_reported_extraction_work_survives_resume_when_output_boundaries_differ(
    pdf_run, monkeypatch, mode, observation_field,
):
    arguments, fetches, extractor, _segmenter, _content = pdf_run
    limits = replace(arguments["plan"].limits, max_pages_or_frames=7)
    arguments["plan"] = _replan(arguments["plan"], limits=limits, stages=stage_policy(stop_after="capture"))
    if mode is EntryExecutionMode.FROM_CAPTURES:
        with prepare_local_run(**arguments) as initial:
            base = initial.retain(initial.run())
        arguments["plan"] = _replan(arguments["plan"], base_release=base)
    observed = _ObservedWorkExtractor(extractor, observation_field, 7)
    arguments["extractor"] = observed
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(stop_after="extraction", extractor=observed))
    with prepare_local_run(**arguments) as prepared:
        task, entry, checkpoint = _interrupt(prepared, monkeypatch, lambda entry: bool(entry.representations))
        representation = entry.representations[0]
        assert entry.execution_mode is mode
        assert len({boundary.page for boundary in representation.boundaries}) == 3
        assert checkpoint.extraction_observations == {representation.representation_id: 7}
        budget = _restored_budget(entry, checkpoint, limits)
        assert budget.usage.pages_or_frames == 7
        with pytest.raises(LimitExceededError, match="pages or frames"):
            budget.charge_pages_or_frames("next-extraction", 1)
        for observations in ({}, {entry.entry_id: {}}):
            with pytest.raises(IntegrityError, match="observation.*missing"):
                WorkBudget(limits).seed_verified_entries((entry,), {}, observations)
        handoff = prepared.handoff_ref
    with prepare_local_run(**arguments, handoff_ref=handoff) as recovered:
        result = recovered.execute_task(recovered.handoff, task)
        assert not recovered._composition.stores.load(result.output_store).entries[0].failures
    assert fetches == ["primary"]
    assert extractor.calls == 1


@pytest.mark.parametrize("mode,frontier", [
    (EntryExecutionMode.FULL, "extraction"),
    (EntryExecutionMode.FULL, "segmentation"),
    (EntryExecutionMode.FROM_CAPTURES, "extraction"),
    (EntryExecutionMode.FROM_CAPTURES, "segmentation"),
    (EntryExecutionMode.FROM_REPRESENTATIONS, "segmentation"),
])
def test_restart_charges_only_executed_prefix_stages(pdf_run, monkeypatch, mode, frontier):
    arguments, fetches, extractor, segmenter, content = pdf_run
    if mode is not EntryExecutionMode.FULL:
        stop_after = "capture" if mode is EntryExecutionMode.FROM_CAPTURES else "extraction"
        if stop_after == "extraction":
            arguments["extractor"] = extractor
        arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(
            stop_after=stop_after, extractor=arguments.get("extractor"),
        ))
        with prepare_local_run(**arguments) as initial:
            base = initial.retain(initial.run())
        arguments["plan"] = _replan(arguments["plan"], base_release=base)
    arguments.update(extractor=extractor, segmenter=segmenter)
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(
        stop_after="segmentation", extractor=extractor, segmenter=segmenter,
    ))
    with prepare_local_run(**arguments) as prepared:
        predicate = (
            (lambda entry: bool(entry.representations) and not entry.segments)
            if frontier == "extraction" else (lambda entry: bool(entry.segments))
        )
        task, entry, checkpoint = _interrupt(prepared, monkeypatch, predicate)
        assert entry.execution_mode is mode
        assert len(entry.representations) == 1
        assert checkpoint.extraction_complete
        assert len(entry.segments) == (3 if frontier == "segmentation" else 0)
        limits = replace(
            arguments["plan"].limits, max_estimated_bytes=len(content), max_pages_or_frames=3, max_segments=3,
        )
        budget = _restored_budget(entry, checkpoint, limits)
        assert budget.usage.source_bytes == (len(content) if mode is EntryExecutionMode.FULL else 0)
        assert budget.usage.pages_or_frames == (0 if mode is EntryExecutionMode.FROM_REPRESENTATIONS else 3)
        assert budget.usage.segments == (3 if frontier == "segmentation" else 0)
        assert budget.usage.processor_cost == 0
        if mode is EntryExecutionMode.FROM_REPRESENTATIONS:
            budget.charge_pages_or_frames("next-extraction", 3)
        else:
            with pytest.raises(LimitExceededError, match="pages or frames"):
                budget.charge_pages_or_frames("next-extraction", 1)
        if frontier == "segmentation":
            with pytest.raises(LimitExceededError, match="segments"):
                budget.charge_segments("next-representation", 1)
        handoff = prepared.handoff_ref
    with prepare_local_run(**arguments, handoff_ref=handoff) as recovered:
        result = recovered.execute_task(recovered.handoff, task)
        completed = recovered._composition.stores.load(result.output_store).entries[0]
        assert not completed.failures
    assert fetches == ["primary"]
    assert extractor.calls == segmenter.calls == 1


def test_processor_prefix_restart_charges_only_requested_reruns(pdf_run, monkeypatch):
    arguments, fetches, extractor, segmenter, _content = pdf_run
    retry = arguments["retry_policy"]
    unaffected = _CountingProcessor(_description("unchanged", "1", retry))
    original = _CountingProcessor(_description("changed", "1", retry))

    def use_processors(processors, base=None):
        declared = ProcessorSet(tuple(item.description for item in processors))
        declared = ProcessorSet(declared.execution_order)
        arguments.update(extractor=extractor, segmenter=segmenter, processors={
            item.description.processor_id: item for item in processors
        })
        arguments["plan"] = _replan(arguments["plan"], base_release=base, processors=declared, stages=stage_policy(
            extractor=extractor, segmenter=segmenter,
            processor_ids=tuple(item.processor_id for item in declared.execution_order),
        ))

    use_processors((unaffected, original))
    with prepare_local_run(**arguments) as initial:
        base = initial.retain(initial.run())
    changed = _CountingProcessor(_description("changed", "2", retry))
    use_processors((unaffected, changed), base)
    with prepare_local_run(**arguments) as prepared:
        task, entry, checkpoint = _interrupt(prepared, monkeypatch, lambda entry: any(
            record.processor_id == changed.description.processor_id for record in entry.derived_records
        ))
        assert entry.execution_mode is EntryExecutionMode.FROM_SEGMENTS
        assert entry.processor_ids_to_run == (changed.description.processor_id,)
        assert len(checkpoint.processor_results) == 6
        assert len(checkpoint.processor_invocations) == 3
        budget = _restored_budget(entry, checkpoint, replace(arguments["plan"].limits, max_processor_cost=3))
        assert budget.usage.source_bytes == budget.usage.pages_or_frames == budget.usage.segments == 0
        assert budget.usage.processor_cost == 3
        with pytest.raises(LimitExceededError, match="processor cost"):
            budget.charge_processor("next-invocation")
        handoff = prepared.handoff_ref
    with prepare_local_run(**arguments, handoff_ref=handoff) as recovered:
        result = recovered.execute_task(recovered.handoff, task)
        assert not recovered._composition.stores.load(result.output_store).entries[0].failures
    assert fetches == ["primary"]
    assert extractor.calls == segmenter.calls == 1
    assert len(unaffected.calls) == len(original.calls) == len(changed.calls) == 3
