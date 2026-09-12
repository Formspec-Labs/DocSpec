"""Inspection explains persisted work without executing plugins or guessing costs."""

from dataclasses import fields, replace

import pytest

from docspec.application.execution_evidence import put_receipt
from docspec.application.inspection_evidence import ENTRY_COUNT_KEYS, entry_evidence
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.jobs import EntryExecutionMode
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorSet
from docspec.errors import IntegrityError
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
from tests.helpers import SharedFixtureContentFetcher
from tests.support.processors import _CountingProcessor, _description
from tests.support.profiles import _seeded_local_run


def _replan(plan, **changes):
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


def _arguments(tmp_path):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(path))
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    return arguments


def _execute(prepared):
    task = next(prepared.task_source(prepared.handoff))
    result = prepared.execute_task(prepared.handoff, task)
    assert result.output_store is not None
    entry = prepared._composition.stores.load(result.output_store).entries[0]
    return entry, result


@pytest.fixture
def saved_run(tmp_path):
    arguments = _arguments(tmp_path)
    with prepare_local_run(**arguments) as prepared:
        entry, result = _execute(prepared)
        assert not entry.failures
        yield arguments, prepared, entry, result


def test_inspection_counts_saved_work_without_plugins_and_bounds_samples(saved_run, monkeypatch):
    arguments, prepared, entry, _ = saved_run

    def unavailable(*args, **kwargs):
        raise AssertionError("inspection must not execute or select a plugin")

    monkeypatch.setattr("docspec.application.execution_checkpoints.EntryCheckpointVerifier.verify_entry", unavailable)
    monkeypatch.setattr(prepared._composition.executor._extractor, "selected_identity", unavailable)
    monkeypatch.setattr(prepared._composition.executor._segmenter, "selected_identity", unavailable)
    evidence = entry_evidence(entry, arguments["plan"], prepared._composition.controls, sample_limit=0)
    assert set(evidence["counts"]) == set(ENTRY_COUNT_KEYS)
    assert evidence["counts"]["capturedFiles"] == evidence["counts"]["newCapturedFiles"] == 1
    assert evidence["counts"]["reusedCapturedBytes"] == 0
    assert evidence["counts"]["newRepresentations"] == evidence["counts"]["newSegments"] == 1
    assert evidence["counts"]["recordedProcessorAttempts"] == 1
    assert evidence["processors"]["sample"] == evidence["processors"]["attemptSample"] == []
    assert evidence["processors"]["sampleTruncated"]
    assert evidence["processors"]["attemptSampleTruncated"]
    assert evidence["stages"]["processing"]["status"] == "recorded-complete"
    assert evidence["stages"]["extraction"]["receiptObservations"]["representationsWithoutObservation"] == 1
    assert evidence["unavailable"]["monetaryCost"]
    assert evidence["unavailable"]["totalExecutionMilliseconds"]
    assert evidence["unavailable"]["replayability"]


@pytest.mark.parametrize("recorded_call", [False, True])
def test_cached_result_origin_does_not_erase_recorded_execution(saved_run, recorded_call):
    arguments, prepared, entry, _ = saved_run
    controls = prepared._composition.controls
    refs = []
    for reference in entry.stage_receipts:
        value = controls.load(reference)
        if value["format"] == "docspec-processor-attempt-receipt":
            if not recorded_call:
                continue
            value = value | {"elapsedMilliseconds": 17}
            reference = put_receipt(controls, "processor-attempt-receipts", "processor-attempt-receipt", value)
        elif value["format"] == "docspec-processor-invocation-receipt":
            # Both are legitimate runtime outcomes: an initial hit, or a hit
            # returned by put-if-absent after a concurrent producer won the cache.
            value = value | {"cacheDisposition": "hit"}
            reference = put_receipt(controls, "processor-invocation-receipts", "processor-invocation-receipt", value)
        refs.append(reference)
    evidence = entry_evidence(replace(entry, stage_receipts=tuple(refs)), arguments["plan"], controls)
    assert evidence["counts"]["processorCacheHits"] == 1
    assert evidence["counts"]["recordedProcessorAttempts"] == int(recorded_call)
    assert evidence["counts"]["recordedProcessorElapsedMilliseconds"] == (17 if recorded_call else 0)
    assert evidence["processors"]["sample"][0]["recordedAttempts"] == int(recorded_call)
    reported = evidence["processors"]["resultReportedResourceUseByOrigin"]
    assert reported["cache"]["results"] == 1
    assert reported["cache"]["inputBytes"] > 0
    assert reported["this-run"]["results"] == 0


def test_capture_then_processing_reports_reused_files_and_new_downstream_work(tmp_path):
    arguments = _arguments(tmp_path)
    original = arguments["plan"]
    arguments["plan"] = _replan(original, stages=stage_policy(stop_after="capture"), processors=ProcessorSet(()))
    with prepare_local_run(**arguments) as capture:
        entry, result = _execute(capture)
        evidence = entry_evidence(entry, arguments["plan"], capture._composition.controls)
        assert evidence["stages"]["capture"]["status"] == "recorded-complete"
        assert evidence["stages"]["extraction"]["status"] == "not-requested"
        assert evidence["stages"]["extraction"]["origin"] is None
        assert evidence["stages"]["segmentation"]["status"] == "not-requested"
        assert evidence["stages"]["processing"]["status"] == "not-requested"
        base = capture.retain(capture.reconcile((result,)))
    arguments["plan"] = _replan(original, base_release=base)
    with prepare_local_run(**arguments) as processing:
        entry, _ = _execute(processing)
        evidence = entry_evidence(entry, arguments["plan"], processing._composition.controls)
        assert entry.execution_mode is EntryExecutionMode.FROM_CAPTURES
        assert evidence["counts"]["newCapturedBytes"] == 0
        assert evidence["counts"]["reusedCapturedBytes"] > 0
        assert evidence["counts"]["newRepresentations"] == evidence["counts"]["newSegments"] == 1
        assert evidence["counts"]["recordedProcessorAttempts"] == 1


def test_changed_processor_keeps_base_result_use_separate_from_current_calls(tmp_path):
    arguments = _arguments(tmp_path)
    retry = arguments["retry_policy"]
    first = _CountingProcessor(_description("first", "1", retry))
    stable = _CountingProcessor(_description("stable", "1", retry))

    def use_processors(processors, base=None):
        declarations = ProcessorSet(ProcessorSet(tuple(item.description for item in processors)).execution_order)
        arguments["processors"] = {item.description.processor_id: item for item in processors}
        arguments["plan"] = _replan(
            arguments["plan"], base_release=base, processors=declarations,
            stages=stage_policy(processor_ids=tuple(item.processor_id for item in declarations.execution_order)),
        )

    use_processors((first, stable))
    with prepare_local_run(**arguments) as original:
        _, result = _execute(original)
        base = original.retain(original.reconcile((result,)))
    assert len(stable.calls) == 1
    replacement = _CountingProcessor(_description("first", "2", retry))
    use_processors((replacement, stable), base)
    with prepare_local_run(**arguments) as changed:
        entry, _ = _execute(changed)
        evidence = entry_evidence(entry, arguments["plan"], changed._composition.controls)
        assert entry.execution_mode is EntryExecutionMode.FROM_SEGMENTS
        assert evidence["counts"]["newCapturedFiles"] == evidence["counts"]["newRepresentations"] == evidence["counts"]["newSegments"] == 0
        assert evidence["counts"]["processorBaseReuses"] == 1
        assert evidence["counts"]["recordedProcessorAttempts"] == 1
        reported = evidence["processors"]["resultReportedResourceUseByOrigin"]
        assert reported["reused-base"]["results"] == reported["this-run"]["results"] == 1
    assert len(stable.calls) == len(replacement.calls) == 1


def test_terminal_processor_failure_exposes_recorded_attempt_without_inventing_result_cost(tmp_path, monkeypatch):
    arguments = _arguments(tmp_path)
    processor = ContentStatisticsProcessor()

    def fail(*args, **kwargs):
        raise ValueError("bad input")

    monkeypatch.setattr(processor, "process", fail)
    arguments["processors"] = {processor.description.processor_id: processor}
    with prepare_local_run(**arguments) as prepared:
        entry, _ = _execute(prepared)
        evidence = entry_evidence(entry, arguments["plan"], prepared._composition.controls)
        assert evidence["counts"]["recordedProcessorFailures"] == 1
        assert evidence["counts"]["processorInvocations"] == 0
        assert evidence["counts"]["failures"] > 0
        assert evidence["stages"]["processing"]["status"] == "not-completed"
        assert evidence["processors"]["attemptSample"][0]["outcome"] == "failed"
        assert all(group["results"] == 0 for group in evidence["processors"]["resultReportedResourceUseByOrigin"].values())


def test_page_observations_come_from_receipt_metadata_not_output_count(saved_run):
    arguments, prepared, entry, _ = saved_run
    controls = prepared._composition.controls
    refs = []
    for reference in entry.stage_receipts:
        receipt = controls.load(reference)
        if receipt["format"] == "docspec-extraction-receipt":
            receipt = receipt | {"metadata": receipt["metadata"] | {"pageCount": 7}}
            reference = put_receipt(controls, "extraction-receipts", "extraction-receipt", receipt)
        refs.append(reference)
    evidence = entry_evidence(replace(entry, stage_receipts=tuple(refs)), arguments["plan"], controls)
    assert evidence["counts"]["representations"] == 1
    assert evidence["stages"]["extraction"]["receiptObservations"] == {
        "pagesOrFrames": 7, "representationsWithObservation": 1, "representationsWithoutObservation": 0,
    }


def test_receipt_identity_and_output_mismatch_are_refused(saved_run):
    arguments, prepared, entry, _ = saved_run
    controls = prepared._composition.controls
    reference = entry.stage_receipts[0]
    receipt = controls.load(reference)
    wrong_identity = controls.put(kind="extraction-receipts", artifact_id="wrong-receipt", value=receipt)
    with pytest.raises(IntegrityError, match="semantic identity"):
        entry_evidence(replace(entry, stage_receipts=(wrong_identity, *entry.stage_receipts[1:])), arguments["plan"], controls)
    changed = put_receipt(controls, "extraction-receipts", "extraction-receipt", receipt | {"outputByteSize": 999})
    with pytest.raises(IntegrityError, match="differs from its immutable output"):
        entry_evidence(replace(entry, stage_receipts=(changed, *entry.stage_receipts[1:])), arguments["plan"], controls)
