"""Exercise typed full-processing lifecycle composition without request files."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import pytest

from docspec.adapters.storage import LocalJsonControlRepository
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.execution import summarize_store_tasks
from docspec.domain.jobs import StoreState
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.errors import IntegrityError, LimitExceededError, ProfileError
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


@pytest.fixture
def arguments(tmp_path: Path):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    request = _local_run_request(path)
    values = _local_run_arguments(request)
    # Input files seed an existing fixture only; the Python lifecycle has the
    # domain values and must not need either file after this point.
    path.unlink()
    request["plan"].unlink()
    return values


def _changed_plan(plan: ProcessingPlan, **changes) -> ProcessingPlan:
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


class _ObservedStatistics(ContentStatisticsProcessor):
    def __init__(self) -> None:
        original = ContentStatisticsProcessor()
        super().__init__(item_limits=replace(original.description.item_limits, max_output_bytes=128 * 1024))
        self.calls = 0

    def process(self, *args):
        self.calls += 1
        return super().process(*args)


def test_typed_run_injects_pinned_processor_and_recovers_without_request_files(arguments, monkeypatch) -> None:
    processor = _ObservedStatistics()
    plan = arguments["plan"]
    arguments["plan"] = _changed_plan(
        plan, processors=ProcessorSet((processor.description,)),
        stages=replace(plan.stages, processor_ids=(processor.description.processor_id,)),
    )
    fetcher = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    fetch_calls = []
    original_fetch = fetcher.fetch

    def observe_fetch(*args, **kwargs):
        fetch_calls.append(args)
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", observe_fetch)
    arguments.update(content_fetcher=fetcher, processors={processor.description.processor_id: processor})
    prepared = prepare_local_run(**arguments)
    task = next(prepared.task_source(prepared.handoff))
    result = prepared.execute_task(prepared.handoff, task)
    assert len(fetch_calls) == processor.calls == 1
    recovered = prepare_local_run(**arguments, handoff_ref=prepared.handoff_ref)
    assert recovered == prepared
    assert recovered.execute_task(recovered.handoff, task) == result
    reference = recovered.reconcile((result,))
    assert recovered.run() == reference
    assert len(fetch_calls) == processor.calls == 1
    receipt = RunReceipt.from_dict(LocalJsonControlRepository(arguments["workspace"].roots["controlRepository"]).load(reference))
    assert receipt.selected_item_count == receipt.store_count == 1
    assert receipt.failures == {"counts": {}, "first": None}
    retained = recovered.retain(reference)
    assert recovered.retain(reference) == retained
    assert recovered._composition.catalog.open(retained).run_receipt == reference
    assert recovered._composition.catalog.current() is None


@pytest.mark.parametrize("stop_after", ["capture", "extraction"])
def test_unrequested_default_stages_are_not_constructed(arguments, monkeypatch, stop_after):
    stages = stage_policy(stop_after=stop_after)
    arguments["plan"] = _changed_plan(arguments["plan"], stages=stages, processors=ProcessorSet(()))

    def unexpected():
        raise AssertionError("an unrequested stage default was constructed")

    monkeypatch.setattr("docspec.runtime.composition.DefaultSegmenterRegistry", unexpected)
    monkeypatch.setattr("docspec.runtime.composition.ContentStatisticsProcessor", unexpected)
    monkeypatch.setattr("docspec.runtime.composition.LocalSqliteProcessorResultCache", unexpected)
    if stop_after == "capture":
        monkeypatch.setattr("docspec.runtime.composition.DefaultExtractorRegistry", unexpected)
    assert stage_policy(stop_after=stop_after) == stages
    with prepare_local_run(**arguments) as prepared:
        assert prepared.handoff.expected_task_count == 1


@pytest.mark.parametrize("supplied", ["extractor", "segmenter"])
def test_capture_run_refuses_unrequested_stage_objects_before_writing(arguments, supplied):
    from docspec.processing.extraction import TextExtractor
    from docspec.processing.segmentation import ParagraphSegmenter

    arguments["plan"] = _changed_plan(
        arguments["plan"], stages=stage_policy(stop_after="capture"), processors=ProcessorSet(()),
    )
    arguments[supplied] = TextExtractor() if supplied == "extractor" else ParagraphSegmenter()
    with pytest.raises(ProfileError, match="not requested"):
        prepare_local_run(**arguments)
    for name in ("controlRepository", "documentStores", "recordStorage", "blobStorage", "documentCatalog", "reconciliation"):
        assert not arguments["workspace"].roots[name].exists(), name


@pytest.mark.parametrize("change", [
    "network_bound", "worker_concurrency", "deadline", "completion_clock", "extractor", "segmenter",
    "missing_processor", "wrong_processor", "wrong_processor_key", "source_acceptance", "document_acceptance",
])
def test_invalid_runtime_choices_refuse_before_storage_or_planning(arguments, change: str) -> None:
    plan = arguments["plan"]
    if change == "network_bound":
        arguments["execution_limits"] = replace(arguments["execution_limits"], max_network_bytes_per_task=1)
    elif change == "worker_concurrency":
        arguments["execution_limits"] = replace(arguments["execution_limits"], max_concurrency_per_worker=2)
    elif change == "deadline":
        arguments["deadline_epoch_seconds"] = 0
    elif change == "completion_clock":
        arguments["completed_at"] = "not-a-clockZ"
    elif change == "extractor":
        arguments["plan"] = _changed_plan(plan, stages=replace(plan.stages, extractor_id="custom-extractor"))
    elif change == "segmenter":
        arguments["plan"] = _changed_plan(plan, stages=replace(plan.stages, segmenter_id="custom-segmenter"))
    elif change == "missing_processor":
        arguments["processors"] = {}
    elif change == "wrong_processor":
        processor = _ObservedStatistics()
        arguments["processors"] = {processor.description.processor_id: processor}
    elif change == "wrong_processor_key":
        arguments["processors"] = {"wrong-key": ContentStatisticsProcessor()}
    elif change == "source_acceptance":
        arguments["source_catalog_producer"] = None
    elif change == "document_acceptance":
        arguments["document_release_producer"] = None
    with pytest.raises(ProfileError):
        prepare_local_run(**arguments)
    for name in ("controlRepository", "documentStores", "recordStorage", "blobStorage", "documentCatalog", "reconciliation"):
        assert not arguments["workspace"].roots[name].exists(), name


@pytest.mark.parametrize("change", ["limits", "deadline"])
def test_recovery_refuses_changed_execution_settings(arguments, change: str) -> None:
    prepared = prepare_local_run(**arguments)
    if change == "limits":
        arguments["execution_limits"] = replace(arguments["execution_limits"], worker_count=2)
    else:
        arguments["deadline_epoch_seconds"] += 1
    with pytest.raises(IntegrityError, match="saved execution settings differ"):
        prepare_local_run(**arguments, handoff_ref=prepared.handoff_ref)


def test_task_operations_require_the_prepared_handoff_and_active_deadline(arguments) -> None:
    arguments["deadline_epoch_seconds"] = 1
    prepared = prepare_local_run(**arguments)
    task = next(prepared.task_source(prepared.handoff))
    other = replace(prepared.handoff, operation_id="another-operation")
    with pytest.raises(IntegrityError, match="different execution handoff"):
        next(prepared.task_source(other))
    with pytest.raises(IntegrityError, match="different execution handoff"):
        prepared.execute_task(other, task)
    recovered = prepare_local_run(**arguments, handoff_ref=prepared.handoff_ref)
    with pytest.raises(LimitExceededError, match="deadline has expired"):
        recovered.execute_task(recovered.handoff, task)


def test_recovery_refuses_a_consistent_handoff_for_a_different_operation(arguments) -> None:
    prepared = prepare_local_run(**arguments)
    operation = "another-operation/v1"
    task_count, task_digest = summarize_store_tasks(
        replace(task, operation_id=operation) for task in prepared.task_source(prepared.handoff)
    )
    other = replace(
        prepared.handoff, operation_id=operation,
        expected_task_count=task_count, task_set_digest=task_digest,
    )
    controls = LocalJsonControlRepository(arguments["workspace"].roots["controlRepository"])
    reference = controls.put(
        kind="execution-handoffs", artifact_id=other.handoff_id, value=other.to_dict(),
    )
    with pytest.raises(IntegrityError, match="unsupported local operation"):
        prepare_local_run(**arguments, handoff_ref=reference)


@pytest.mark.parametrize("state", (StoreState.RUNNING, StoreState.SEALED))
def test_task_recovery_executes_only_an_unfinished_real_store(arguments, monkeypatch, state) -> None:
    fetcher = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    with prepare_local_run(**arguments, content_fetcher=fetcher) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        composition = prepared._composition
        if state is StoreState.RUNNING:
            current = composition.stores.save(composition.stores.load(task.input_store).start("interrupted-attempt"))
        else:
            current = prepared.execute_task(prepared.handoff, task).output_store
        execute, deliver = composition.executor.execute_store, composition.delivery.deliver_store
        executor_calls, delivery_calls = [], []

        def observed_execute(reference):
            executor_calls.append(reference)
            return execute(reference)

        def observed_deliver(reference, sink):
            delivery_calls.append(reference)
            return deliver(reference, sink)

        monkeypatch.setattr(composition.executor, "execute_store", observed_execute)
        monkeypatch.setattr(composition.delivery, "deliver_store", observed_deliver)
        result = prepared.execute_task(prepared.handoff, task)
        assert result.task.input_store == task.input_store
        assert composition.stores.load(result.output_store).state is StoreState.SEALED
        if state is StoreState.SEALED:
            assert executor_calls == []
            assert delivery_calls == [current]
            assert result.output_store == current
        else:
            assert executor_calls == [current]
            assert len(delivery_calls) == 1
            assert delivery_calls[0].revision > current.revision
            assert result.output_store.revision > delivery_calls[0].revision


@pytest.mark.parametrize("changed_stage", ["extractor", "segmenter"])
def test_injected_stages_run_and_recover_only_with_their_pinned_settings(arguments, changed_stage: str) -> None:
    from docspec.domain.identity import identity_digest
    from docspec.processing.extraction import TextExtractor
    from docspec.processing.segmentation import ParagraphSegmenter
    from tests.support.processors import _CountingExtractor, _CountingSegmenter

    extractor = _CountingExtractor(TextExtractor())
    segmenter = _CountingSegmenter(ParagraphSegmenter())
    original = arguments["plan"]
    arguments["plan"] = _changed_plan(original, stages=stage_policy(
        extractor=extractor, segmenter=segmenter, processor_ids=original.stages.processor_ids,
    ))
    arguments.update(
        extractor=extractor, segmenter=segmenter,
        content_fetcher=SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"]),
    )
    with prepare_local_run(**arguments) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        result = prepared.execute_task(prepared.handoff, task)
        handoff_ref = prepared.handoff_ref
    assert extractor.calls == segmenter.calls == 1
    with prepare_local_run(**arguments, handoff_ref=handoff_ref) as recovered:
        assert recovered.execute_task(recovered.handoff, task) == result
    assert extractor.calls == segmenter.calls == 1
    if changed_stage == "extractor":
        extractor.delegate.configuration_digest = identity_digest({"different": "extraction"})
    else:
        segmenter.delegate.policy_digest = identity_digest({"different": "segmentation"})
    with pytest.raises(ProfileError, match="settings differ"):
        prepare_local_run(**arguments, handoff_ref=handoff_ref)
    assert extractor.calls == segmenter.calls == 1


def test_zero_task_recovery_still_checks_stage_configuration(arguments, tmp_path: Path) -> None:
    from docspec.domain.content import SourceItem, SourceItemState
    from docspec.domain.identity import identity_digest
    from docspec.processing.extraction import TextExtractor
    from docspec.processing.segmentation import ParagraphSegmenter
    from tests.helpers import write_shared_source_catalog
    from docspec.workspace import LocalWorkspace

    root = tmp_path / "deleted-source-catalog"
    source = write_shared_source_catalog(root, (SourceItem("deleted", "1", (), state=SourceItemState.DELETED),))
    workspace = arguments["workspace"]
    arguments["workspace"] = LocalWorkspace(workspace.root, workspace.roots | {"sourceCatalog": root})
    extractor, segmenter = TextExtractor(), ParagraphSegmenter()
    arguments["plan"] = _changed_plan(arguments["plan"], source_catalog=source, selection={"excludeItemIds": ["deleted"]}, stages=stage_policy(
        extractor=extractor, segmenter=segmenter, processor_ids=arguments["plan"].stages.processor_ids,
    ))
    arguments.update(extractor=extractor, segmenter=segmenter)
    with prepare_local_run(**arguments) as prepared:
        assert prepared.handoff.expected_task_count == 0
        handoff_ref = prepared.handoff_ref
    extractor.configuration_digest = identity_digest({"different": "zero-task-settings"})
    with pytest.raises(ProfileError, match="settings differ"):
        prepare_local_run(**arguments, handoff_ref=handoff_ref)
