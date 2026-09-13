"""Failed-item planning uses bounded retained evidence and relevant retry intent."""

from collections import Counter
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from docspec.application.failure_frontier import _payloads, failed_item_frontier, spool_failed_evidence
from docspec.application.planner import RunPlanner, _CompiledSelection
from docspec.domain.content import SourceItem
from docspec.domain.identity import identity_digest
from docspec.domain.jobs import EntryExecutionMode, FailureClass, FailureRecord
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.segmentation import ParagraphSegmenter
from tests.support.processors import _CountingProcessor, _description
from tests.support.experiments import _FailingProcessor, experiment as _experiment_fixture

experiment = _experiment_fixture


def _frontier(view, *, reader=None, plan=None):
    source = SourceItem.from_dict(tuple(view.records("source-items"))[0]["payload"])
    disposition = tuple(view.records("dispositions"))[0]["payload"]
    with view._workspace_factory.create() as workspace:
        workspace.add_record("states", identity=source.item_id, source_item_id=source.item_id, record={
            "terminalFailure": FailureRecord(
                FailureClass.DETERMINISTIC_INPUT, "test.late-failure", "after all output", 1, False,
            ).to_dict(),
        })
        spool_failed_evidence(reader or view._reader, workspace, item_state_collection="states")
        return failed_item_frontier(
            source, view.plan.stages, disposition["entryId"], workspace, view._controls, plan or view.plan,
        )


@pytest.mark.parametrize("value", [True, [], None, "all", "permanent"])
def test_retry_selection_refuses_ambiguous_values(value):
    with pytest.raises(IntegrityError, match="retryFailures"):
        _CompiledSelection.compile({"retryFailures": value}, partition_count=1)


def test_failure_evidence_scan_releases_its_reader_on_invalid_rows():
    closed = []

    def records():
        try:
            yield {"invalid": "row"}
        finally:
            closed.append(True)

    retained_iterator = records()
    reader = SimpleNamespace(
        release=SimpleNamespace(active_layers=(SimpleNamespace(layer_kind="files"),)),
        scan=lambda **_: retained_iterator,
    )
    with pytest.raises(IntegrityError, match="invalid live record"):
        spool_failed_evidence(reader, None, item_state_collection="states")
    assert closed == [True]


def test_failure_evidence_bound_releases_its_scratch_reader():
    closed = []

    def records():
        try:
            yield {"first": "row"}
            yield {"second": "row"}
        finally:
            closed.append(True)

    retained_iterator = records()
    workspace = SimpleNamespace(stream_records=lambda _: retained_iterator)
    with pytest.raises(LimitExceededError, match="failed-item work bound"):
        tuple(_payloads(workspace, "item", "files", 1))
    assert closed == [True]


def test_independent_processor_change_holds_failure_and_mixed_plan_repair_uses_original_receipts(experiment):
    run, retry, fetches, extractor, segmenter = experiment
    upstream = _CountingProcessor(_description("upstream", "1", retry))
    failing = _FailingProcessor(_description("failing", "1", retry,
        dependencies=(upstream.description.processor_id,)))
    independent = _CountingProcessor(_description("independent", "1", retry))
    base, failed, _ = run(processors=(upstream, failing, independent))
    changed = _CountingProcessor(_description("independent", "2", retry))
    skipped_base, skipped, entries = run(processors=(upstream, failing, changed), base_release=base)
    assert entries == ()
    assert changed.calls == []
    assert tuple(skipped.records("dispositions")) == tuple(failed.records("dispositions"))
    failing.fail = False
    _, repaired, entries = run(
        processors=(upstream, failing, changed), base_release=skipped_base,
        selection={"retryFailures": "selected"},
    )
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert set(entries[0].processor_ids_to_run) == {failing.description.processor_id, changed.description.processor_id}
    assert len(upstream.calls) == 2 and len(changed.calls) == 2
    assert (len(fetches), extractor.calls, segmenter.calls) == (1, 1, 1)
    assert tuple(repaired.records("dispositions"))[0]["payload"]["terminalFailure"] is None


def test_planning_scans_each_retained_layer_once_without_source_lookups(experiment, monkeypatch):
    run, retry, *_ = experiment
    failing = _FailingProcessor(_description("failing", "1", retry))
    base, _, _ = run(processors=(failing,))
    scans = Counter()
    original = RunPlanner._open_base_reader

    class SinglePassReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.release = wrapped.release

        def scan(self, *, layer_kind):
            scans[layer_kind] += 1
            assert scans[layer_kind] == 1
            yield from self.wrapped.scan(layer_kind=layer_kind)

        def scan_source(self, **kwargs):
            pytest.fail("planning must not rescan a catalog layer for each source")

        def lookup(self, **kwargs):
            pytest.fail("planning must use its bounded scratch index")

    def open_reader(self, reference):
        return SinglePassReader(original(self, reference))

    monkeypatch.setattr(RunPlanner, "_open_base_reader", open_reader)
    failing.fail = False
    run(processors=(failing,), base_release=base, selection={"retryFailures": "selected"})
    assert scans["receipts"] == scans["files"] == scans["representations"] == scans["segments"] == 1


def test_promised_representation_without_its_receipt_refuses_instead_of_rebuilding(experiment):
    run, retry, *_ = experiment
    failing = _FailingProcessor(_description("failing", "1", retry))
    _, view, _ = run(processors=(failing,))

    class MissingReceipt:
        release = view._reader.release

        def scan(self, *, layer_kind):
            for row in view._reader.scan(layer_kind=layer_kind):
                if layer_kind == "receipts":
                    receipt = view._controls.load(ArtifactRef.from_dict(row["payload"]["artifact"]))
                    if receipt["format"] == "docspec-extraction-receipt":
                        continue
                yield row

    with pytest.raises(IntegrityError, match="do not cover its representations"):
        _frontier(view, reader=MissingReceipt())


def test_complete_empty_segmentation_and_late_failure_can_reuse_all_output(experiment):
    run, retry, *_ = experiment

    class EmptySegmenter(ParagraphSegmenter):
        segmenter_id = "tests.empty-segments/v1"
        policy_digest = identity_digest({"segments": "none"})

        def segment(self, representation):
            return ()

    processor = _CountingProcessor(_description("empty-input", "1", retry))
    _, view, _ = run(processors=(processor,), segmenter=EmptySegmenter())
    frontier = _frontier(view)
    assert frontier.capture_complete and frontier.extraction_complete and frontier.segmentation_complete
    assert frontier.completed_processors == view.plan.stages.processor_ids
    assert frontier.unfinished_processor is None
    assert processor.calls == []
    impact = RunPlanner._failed_impact(frontier, view.plan.stages, view.plan)
    assert impact.execution_mode is EntryExecutionMode.FROM_SEGMENTS and impact.processor_ids == ()
    changed = replace(view.plan.stages, segmenter_policy_digest="sha256:" + "a" * 64)
    assert not frontier.changed_inputs(view.plan.stages, changed)


def test_nonempty_complete_processor_output_has_no_unfinished_node(experiment):
    run, retry, *_ = experiment
    processor = _CountingProcessor(_description("complete", "1", retry))
    _, view, _ = run(processors=(processor,))
    frontier = _frontier(view)
    assert len(processor.calls) == 2
    assert frontier.unfinished_processor is None
    assert RunPlanner._failed_impact(frontier, view.plan.stages, view.plan).processor_ids == ()


def test_failed_frontier_observation_respects_the_planned_segment_bound(experiment):
    run, retry, *_ = experiment
    failing = _FailingProcessor(_description("failing", "1", retry))
    _, view, _ = run(processors=(failing,))
    values = {field.name: getattr(view.plan, field.name) for field in fields(view.plan) if field.name != "plan_id"}
    smaller = ProcessingPlan.create(**(values | {"limits": replace(view.plan.limits, max_segments=1)}))
    with pytest.raises(LimitExceededError, match="failed-item work bound"):
        _frontier(view, plan=smaller)
