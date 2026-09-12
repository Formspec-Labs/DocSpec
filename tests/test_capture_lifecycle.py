"""Capture-first runs retain exact inputs for later stages and verified recovery."""

from dataclasses import fields, replace

import pytest

from docspec.application.work_budget import WorkBudget
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import EntryExecutionMode, StoreState
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.errors import IntegrityError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
from docspec.workspace import LocalWorkspace
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.support.profiles import _seeded_local_run
from tests.support.incremental import _active_document_state


def _replan(plan, **changes):
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


@pytest.fixture
def capture_arguments(tmp_path, monkeypatch, request):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(path))
    workspace = arguments["workspace"]
    candidates = []
    for name, content in (("z-first", b"First document."), ("a-second", b"Second document.")):
        (workspace.roots["sourceContent"] / name).write_bytes(content)
        candidates.append(CandidateFile(
            name, name, "text/plain", expected_digest=sha256_digest(content),
            expected_size=len(content), transport_version="test:v1",
        ))
    catalog_root = tmp_path / "two-candidate-catalog"
    names = ("document-a", "document-b") if getattr(request, "param", 1) == 2 else ("two-files",)
    source = write_shared_source_catalog(catalog_root, tuple(
        SourceItem(name, "v1", tuple(candidates), metadata={"expectedSegments": 2}) for name in names
    ))
    arguments["workspace"] = LocalWorkspace(workspace.root, workspace.roots | {"sourceCatalog": catalog_root})
    arguments["plan"] = _replan(
        arguments["plan"], source_catalog=source, stages=stage_policy(stop_after="capture"),
        processors=ProcessorSet(()),
    )
    fetcher = SharedFixtureContentFetcher(workspace.roots["sourceContent"])
    calls = []
    fetch = fetcher.fetch

    def observed(candidate, **kwargs):
        calls.append(candidate.candidate_id)
        return fetch(candidate, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", observed)
    arguments["content_fetcher"] = fetcher
    return arguments, calls


def _finish(prepared):
    task = next(prepared.task_source(prepared.handoff))
    result = prepared.execute_task(prepared.handoff, task)
    entry = prepared._composition.stores.load(result.output_store).entries[0]
    assert not entry.failures
    return entry, prepared.retain(prepared.reconcile((result,)))


def test_capture_then_process_reuses_all_files_and_keeps_acquisition_evidence(capture_arguments, monkeypatch):
    arguments, calls = capture_arguments

    def unused(*args, **kwargs):
        raise AssertionError("capture-only must not construct downstream stages")

    with monkeypatch.context() as isolated:
        isolated.setattr("docspec.runtime.composition.DefaultExtractorRegistry", unused)
        isolated.setattr("docspec.runtime.composition.DefaultSegmenterRegistry", unused)
        with prepare_local_run(**arguments) as capture:
            captured, base = _finish(capture)
            receipt = RunReceipt.from_dict(capture._composition.controls.load(capture.run()))
            counts = {layer.layer_kind: layer.record_count for layer in receipt.staged_layers}
            assert counts["files"] == 2
            assert counts["representations"] == counts["segments"] == 0
            assert not captured.stage_receipts
    assert calls == ["z-first", "a-second"]
    original_fetch = arguments["content_fetcher"].fetch
    monkeypatch.setattr(arguments["content_fetcher"], "fetch", unused)
    extractor, segmenter = TextExtractor(), ParagraphSegmenter()
    arguments.update(extractor=extractor, segmenter=segmenter)
    arguments["plan"] = _replan(arguments["plan"], base_release=base, stages=stage_policy(
        extractor=extractor, segmenter=segmenter,
    ))
    with prepare_local_run(**arguments) as processing:
        processed, result = _finish(processing)
        assert processed.execution_mode is EntryExecutionMode.FROM_CAPTURES
        assert processed.captured_files == captured.captured_files
        assert len(processed.representations) == len(processed.segments) == 2
        assert result != base
        reused_state = _active_document_state(processing._composition.catalog, result)
        budget = WorkBudget(arguments["plan"].limits)
        checkpoint = processing._composition.executor._checkpoints.verify_entry(processed, arguments["plan"])
        for _ in range(2):
            budget.seed_verified_entries(
                (processed,),
                {processed.entry_id: checkpoint.processor_invocations},
                {processed.entry_id: checkpoint.extraction_observations},
            )
        assert budget.usage.source_bytes == 0
        assert budget.usage.segments == 2
        reader = processing._composition.catalog.open_reader(base)
        assert tuple(reader.scan(layer_kind="representations")) == ()
    monkeypatch.setattr(arguments["content_fetcher"], "fetch", original_fetch)
    arguments["plan"] = _replan(arguments["plan"], base_release=None)
    with prepare_local_run(**arguments) as clean:
        _, clean_result = _finish(clean)
        assert _active_document_state(clean._composition.catalog, clean_result) == reused_state


def test_capture_only_resumes_after_each_durable_candidate(capture_arguments, monkeypatch):
    arguments, calls = capture_arguments

    class WorkerStopped(BaseException):
        pass

    with prepare_local_run(**arguments) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        stores = prepared._composition.stores
        save = stores.save

        def stop_after_capture(store):
            reference = save(store)
            if store.state is StoreState.RUNNING and len(store.entries[0].captured_files) == 1:
                raise WorkerStopped()
            return reference

        monkeypatch.setattr(stores, "save", stop_after_capture)
        with pytest.raises(WorkerStopped):
            prepared.execute_task(prepared.handoff, task)
        handoff = prepared.handoff_ref
    assert calls == ["z-first"]
    with prepare_local_run(**arguments, handoff_ref=handoff) as recovered:
        entry, _ = _finish(recovered)
        assert len(entry.captured_files) == 2
        assert not entry.representations
    assert calls == ["z-first", "a-second"]


@pytest.mark.parametrize("stop_after", ["capture", "extraction"])
def test_shorter_run_removes_only_its_superseded_descendants(capture_arguments, stop_after):
    arguments, calls = capture_arguments
    extractor, segmenter = TextExtractor(), ParagraphSegmenter()
    arguments.update(extractor=extractor, segmenter=segmenter)
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(extractor=extractor, segmenter=segmenter))
    with prepare_local_run(**arguments) as full:
        original, base = _finish(full)
    arguments.pop("segmenter")
    if stop_after == "capture":
        arguments.pop("extractor")
    arguments["plan"] = _replan(arguments["plan"], base_release=base, stages=stage_policy(
        stop_after=stop_after, extractor=arguments.get("extractor"),
    ))
    with prepare_local_run(**arguments) as shorter:
        entry, result = _finish(shorter)
        assert entry.captured_files == original.captured_files
        assert len(entry.representations) == (2 if stop_after == "extraction" else 0)
        assert not entry.segments
        assert not entry.derived_records
        assert len(tuple(shorter._composition.catalog.open_reader(base).scan(layer_kind="segments"))) == 2
        assert tuple(shorter._composition.catalog.open_reader(result).scan(layer_kind="segments")) == ()
    assert calls == ["z-first", "a-second"]


def test_capture_completion_requires_all_candidates(capture_arguments):
    arguments, _ = capture_arguments
    with prepare_local_run(**arguments) as prepared:
        entry, _ = _finish(prepared)
        verifier = prepared._composition.executor._checkpoints
        with pytest.raises(IntegrityError, match="every planned processing stage"):
            verifier.verify_terminal_entry(replace(entry, captured_files=entry.captured_files[:1]), arguments["plan"])


def test_capture_completion_rejects_an_unrequested_representation(capture_arguments):
    arguments, _ = capture_arguments
    with prepare_local_run(**arguments) as prepared:
        entry, _ = _finish(prepared)
        captured = entry.captured_files[0]
        extraction = TextExtractor().extract(captured, b"First document.")
        forged = replace(entry, representations=(extraction.payload.representation,))
        with pytest.raises(IntegrityError, match="without a requested extractor"):
            prepared._composition.executor._checkpoints.verify_terminal_entry(forged, arguments["plan"])


def test_extraction_completion_requires_receipts_for_every_capture(capture_arguments):
    arguments, _ = capture_arguments
    extractor = TextExtractor()
    arguments["extractor"] = extractor
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(stop_after="extraction", extractor=extractor))
    with prepare_local_run(**arguments) as prepared:
        entry, _ = _finish(prepared)
        assert len(entry.representations) == 2
        assert not entry.segments
        incomplete = replace(entry, representations=(), stage_receipts=())
        with pytest.raises(IntegrityError, match="every planned processing stage"):
            prepared._composition.executor._checkpoints.verify_terminal_entry(incomplete, arguments["plan"])


@pytest.mark.parametrize("capture_arguments", [2], indirect=True)
def test_shortening_selected_documents_preserves_then_retires_inherited_processor_layers(capture_arguments):
    from docspec.processing.processors import ContentStatisticsProcessor

    arguments, calls = capture_arguments
    processor = ContentStatisticsProcessor()
    identifier = processor.description.processor_id
    arguments["plan"] = _replan(arguments["plan"],
        stages=stage_policy(processor_ids=(identifier,)), processors=ProcessorSet((processor.description,)),
    )
    with prepare_local_run(**arguments) as full:
        base = full.retain(full.run())
        original_rows = tuple(full._composition.catalog.scan(base, layer_kind=f"derived:{identifier}"))
        assert len(original_rows) == 4
    for selected, remaining in (("document-a", "document-b"), ("document-b", None)):
        arguments["plan"] = _replan(arguments["plan"], base_release=base,
            stages=stage_policy(stop_after="capture"), processors=ProcessorSet(()),
            selection={"includeItemIds": [selected]},
        )
        with prepare_local_run(**arguments) as shortened:
            base = shortened.retain(shortened.run())
            reader = shortened._composition.catalog.open_reader(base)
            kinds = {layer.layer_kind for layer in reader.release.active_layers}
            if remaining is None:
                assert f"derived:{identifier}" not in kinds
            else:
                rows = tuple(reader.scan(layer_kind=f"derived:{identifier}"))
                assert rows == tuple(row for row in original_rows if row["sourceItemId"] == remaining)
            requested = {
                row["sourceItemId"]: row["payload"]["requestedStages"]["processorIds"]
                for row in reader.scan(layer_kind="dispositions")
            }
            assert requested[selected] == []
            if remaining is not None:
                assert requested[remaining] == [identifier]
    assert len(calls) == 4


@pytest.mark.parametrize("reuse", ["captures", "representations"])
def test_reused_prefix_survives_interruption_after_new_stage_checkpoint(capture_arguments, monkeypatch, reuse):
    from tests.support.processors import _CountingExtractor, _CountingSegmenter
    from docspec.processing.processors import ContentStatisticsProcessor

    arguments, calls = capture_arguments
    extractor = _CountingExtractor(TextExtractor())
    segmenter = _CountingSegmenter(ParagraphSegmenter())
    if reuse == "representations":
        arguments["extractor"] = extractor
        arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(stop_after="extraction", extractor=extractor))
    with prepare_local_run(**arguments) as initial:
        original, base = _finish(initial)
    processor = ContentStatisticsProcessor()
    arguments.update(extractor=extractor, segmenter=segmenter)
    arguments["plan"] = _replan(arguments["plan"], base_release=base,
        stages=stage_policy(extractor=extractor, segmenter=segmenter, processor_ids=(processor.description.processor_id,)),
        processors=ProcessorSet((processor.description,)),
    )

    class WorkerStopped(BaseException):
        pass

    with prepare_local_run(**arguments) as interrupted:
        task = next(interrupted.task_source(interrupted.handoff))
        save = interrupted._composition.stores.save

        def stop_after_new_stage(store):
            reference = save(store)
            entry = store.entries[0]
            reached = len(entry.representations) == 1 if reuse == "captures" else bool(entry.segments)
            if store.state is StoreState.RUNNING and reached:
                raise WorkerStopped()
            return reference

        monkeypatch.setattr(interrupted._composition.stores, "save", stop_after_new_stage)
        with pytest.raises(WorkerStopped):
            interrupted.execute_task(interrupted.handoff, task)
        handoff = interrupted.handoff_ref
    assert extractor.calls == (1 if reuse == "captures" else 2)
    assert segmenter.calls == (0 if reuse == "captures" else 2)
    with prepare_local_run(**arguments, handoff_ref=handoff) as resumed:
        completed, _ = _finish(resumed)
        assert completed.captured_files == original.captured_files
        assert len(completed.derived_records) == 2
    assert calls == ["z-first", "a-second"]
    assert extractor.calls == segmenter.calls == 2
