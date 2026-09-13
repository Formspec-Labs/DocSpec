"""Repair accepted failures through the public lifecycle without repeating valid work."""

import pytest

from docspec.application.processor_runtime import ProcessorRuntime
from docspec.application.work_budget import WorkBudget
from docspec.domain.identity import identity_digest
from docspec.domain.jobs import EntryExecutionMode, FailureClass
from tests.support.checkpoints import _InterruptAfterStageRepository, _WorkerInterrupted
from tests.support.experiments import _FailingProcessor, experiment as _experiment_fixture
from tests.support.processors import _CountingProcessor, _description

experiment = _experiment_fixture


def _payloads(view, kind):
    return tuple(row["payload"] for row in view.records(kind))


def test_partial_processor_repair_reuses_complete_upstream_and_keeps_old_failure(experiment):
    run, retry, fetches, extractor, segmenter = experiment
    upstream = _CountingProcessor(_description("upstream", "1", retry))
    failing = _FailingProcessor(_description("failing", "1", retry,
        dependencies=(upstream.description.processor_id,)), fail_on=2)
    processors = (upstream, failing)
    base, failed, _ = run(processors=processors)
    assert len(upstream.calls) == 2 and len(failing.calls) == 1
    failure = _payloads(failed, "dispositions")[0]["terminalFailure"]
    assert failure["failureClass"] == FailureClass.DETERMINISTIC_INPUT.value
    assert len(_payloads(failed, "failures")) == 1

    # An unchanged successor keeps the accepted failure visible without retrying it.
    _, skipped, entries = run(processors=processors, base_release=base)
    assert entries == ()
    assert _payloads(skipped, "dispositions") == _payloads(failed, "dispositions")
    failing.fail = False
    _, repaired, entries = run(processors=processors, base_release=base, selection={"retryFailures": "selected"})
    assert len(entries) == 1
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert entries[0].processor_ids_to_run == (failing.description.processor_id,)
    assert (len(fetches), extractor.calls, segmenter.calls, len(upstream.calls)) == (1, 1, 1, 2)
    # The incomplete node is scheduled for both segments. Its earlier successful
    # segment may hit the existing exact-input cache; the failed one executes.
    assert failing.attempts == 3
    assert _payloads(repaired, "dispositions")[0]["terminalFailure"] is None
    assert _payloads(repaired, "failures") == ()
    assert _payloads(failed, "dispositions")[0]["terminalFailure"] == failure
    assert len(_payloads(repaired, f"derived:{failing.description.processor_id}")) == 2
    assert _payloads(repaired, f"derived:{upstream.description.processor_id}") == _payloads(
        failed, f"derived:{upstream.description.processor_id}")
    _, clean, _ = run(processors=processors, fresh=True)
    for processor in processors:
        kind = f"derived:{processor.description.processor_id}"
        # Execution receipts identify each attempt separately. The content and
        # schema of each processor output must agree with a clean run.
        assert sorted((row["outputDigest"], row["schemaId"]) for row in _payloads(repaired, kind)) == sorted(
            (row["outputDigest"], row["schemaId"]) for row in _payloads(clean, kind))


@pytest.mark.parametrize("error, expected", [(TimeoutError, True), (ValueError, False)])
def test_transient_retry_selection_uses_the_final_failure_class(experiment, error, expected):
    run, retry, fetches, extractor, segmenter = experiment
    processor = _FailingProcessor(_description("failure", "1", retry), error=error)
    base, _, _ = run(processors=(processor,))
    processor.fail = False
    _, view, entries = run(processors=(processor,), base_release=base, selection={"retryFailures": "transient"})
    assert bool(entries) is expected
    assert (len(fetches), extractor.calls, segmenter.calls) == (1, 1, 1)
    assert (_payloads(view, "dispositions")[0]["terminalFailure"] is None) is expected


def test_removing_failed_processor_uses_complete_prefix_without_explicit_retry(experiment):
    run, retry, fetches, extractor, segmenter = experiment
    processor = _FailingProcessor(_description("failure", "1", retry))
    base, _, _ = run(processors=(processor,))
    _, view, entries = run(base_release=base)
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert entries[0].processor_ids_to_run == ()
    assert _payloads(view, "dispositions")[0]["terminalFailure"] is None
    assert (len(fetches), extractor.calls, segmenter.calls, processor.attempts) == (1, 1, 1, 1)


@pytest.mark.parametrize("stage, mode", [
    ("extraction", EntryExecutionMode.FROM_CAPTURES),
    ("segmentation", EntryExecutionMode.FROM_REPRESENTATIONS),
])
def test_failed_stage_reuses_only_its_complete_predecessors(experiment, monkeypatch, stage, mode):
    run, _, fetches, extractor, segmenter = experiment
    selected, method = (extractor, "extract") if stage == "extraction" else (segmenter, "segment")
    actual = getattr(selected, method)

    def fail(*args, **kwargs):
        raise ValueError("fixture stage refusal")

    monkeypatch.setattr(selected, method, fail)
    base, _, _ = run()
    monkeypatch.setattr(selected, method, actual)
    _, view, entries = run(base_release=base, selection={"retryFailures": "selected"})
    assert entries[0].execution_mode is mode
    assert len(fetches) == extractor.calls == segmenter.calls == 1
    assert _payloads(view, "dispositions")[0]["terminalFailure"] is None
    assert len(_payloads(view, "segments")) == 2


def test_independent_processor_change_keeps_failed_item_but_replacing_failure_admits_repair(experiment):
    run, retry, fetches, extractor, segmenter = experiment
    failing = _FailingProcessor(_description("failed-a", "1", retry))
    other = _CountingProcessor(_description("independent-b", "1", retry))
    base, failed, _ = run(processors=(failing, other))
    changed_other = _CountingProcessor(_description("independent-b", "2", retry))
    _, skipped, entries = run(processors=(failing, changed_other), base_release=base)
    assert entries == () and changed_other.calls == []
    assert _payloads(skipped, "dispositions") == _payloads(failed, "dispositions")
    changed_failure = _CountingProcessor(_description("failed-a", "2", retry))
    _, repaired, entries = run(processors=(changed_failure, changed_other), base_release=base)
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert set(entries[0].processor_ids_to_run) == {
        changed_failure.description.processor_id, changed_other.description.processor_id,
    }
    assert _payloads(repaired, "dispositions")[0]["terminalFailure"] is None
    assert (len(fetches), extractor.calls, segmenter.calls) == (1, 1, 1)


class _EmptySegmenter:
    segmenter_id = "tests.empty-segments/v1"
    policy_digest = identity_digest({"segments": "none"})

    def selected_identity(self, representation):
        return self.segmenter_id, self.policy_digest

    def segment(self, representation):
        return ()


@pytest.mark.parametrize("empty", [False, True])
def test_late_failure_can_reuse_all_complete_outputs_including_empty_segmentation(experiment, monkeypatch, empty):
    run, retry, fetches, extractor, segmenter = experiment
    processor = _CountingProcessor(_description("complete", "1", retry))
    original = ProcessorRuntime.run_graph

    def fail_after_complete(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise TimeoutError("fixture failure after complete graph output")

    choices = {"segmenter": _EmptySegmenter()} if empty else {}
    monkeypatch.setattr(ProcessorRuntime, "run_graph", fail_after_complete)
    base, failed, _ = run(processors=(processor,), **choices)
    assert _payloads(failed, "dispositions")[0]["terminalFailure"] is not None
    calls = len(processor.calls)
    monkeypatch.setattr(ProcessorRuntime, "run_graph", original)
    _, repaired, entries = run(processors=(processor,), base_release=base,
        selection={"retryFailures": "transient"}, **choices)
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert entries[0].processor_ids_to_run == ()
    assert len(processor.calls) == calls == (0 if empty else 2)
    assert _payloads(repaired, "dispositions")[0]["terminalFailure"] is None
    assert len(fetches) == extractor.calls == 1
    assert segmenter.calls == (0 if empty else 1)


def test_interrupted_repair_preserves_planned_work_and_charges_current_invocations_once(experiment, monkeypatch):
    run, retry, fetches, extractor, segmenter = experiment
    upstream = _CountingProcessor(_description("upstream", "1", retry))
    failing = _FailingProcessor(_description("failure", "1", retry,
        dependencies=(upstream.description.processor_id,)))
    processors = (upstream, failing)
    base, _, _ = run(processors=processors)
    failing.fail = False
    saved = {}

    def interrupt(prepared):
        executor = prepared._composition.executor
        repository = _InterruptAfterStageRepository(prepared._composition.stores, lambda store: any(
            record.processor_id == failing.description.processor_id
            for entry in store.entries for record in entry.derived_records
        ))
        monkeypatch.setattr(executor, "_stores", repository)
        saved["handoff"] = prepared.handoff_ref
        task = next(prepared.task_source(prepared.handoff))
        prepared.execute_task(prepared.handoff, task)

    settings = {"processors": processors, "base_release": base, "selection": {"retryFailures": "selected"}}
    with pytest.raises(_WorkerInterrupted):
        run(**settings, on_prepared=interrupt)
    calls_after_interrupt = failing.attempts

    def remember(prepared):
        assert prepared.handoff_ref == saved["handoff"]
        saved["prepared"] = prepared

    _, view, entries = run(**settings, on_prepared=remember)
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert entries[0].processor_ids_to_run == (failing.description.processor_id,)
    assert failing.attempts == calls_after_interrupt
    assert (len(fetches), extractor.calls, segmenter.calls, len(upstream.calls)) == (1, 1, 1, 2)
    assert _payloads(view, "dispositions")[0]["terminalFailure"] is None
    prepared = saved["prepared"]
    task = next(prepared.task_source(prepared.handoff))
    stores = prepared._composition.stores
    entry = stores.load(stores.latest(task.input_store.store_id)).entries[0]
    checkpoint = prepared._composition.executor._checkpoints.verify_entry(entry, prepared.plan)
    budget = WorkBudget(prepared.plan.limits)
    args = ((entry,), {entry.entry_id: checkpoint.processor_invocations},
            {entry.entry_id: checkpoint.extraction_observations})
    budget.seed_verified_entries(*args)
    usage = budget.usage
    budget.seed_verified_entries(*args)
    assert budget.usage == usage
    assert usage.source_bytes == usage.pages_or_frames == usage.segments == 0
    assert usage.processor_cost == 2
