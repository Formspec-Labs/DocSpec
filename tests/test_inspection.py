"""Inspection explains real retained work without rebuilding execution services."""

from dataclasses import fields, replace

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import BlobRef
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.extraction import TextExtractor
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import open_local_inspection, prepare_local_run, stage_policy
from docspec.workspace import LocalWorkspace
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.support.profiles import _seeded_local_run_arguments


def _replan(plan, **changes):
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


@pytest.fixture
def arguments(tmp_path):
    values = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    workspace = values["workspace"]
    content = b"Two retained documents."
    (workspace.roots["sourceContent"] / "input.txt").write_bytes(content)
    candidate = CandidateFile("primary", "input.txt", "text/plain", expected_digest=sha256_digest(content),
                              expected_size=len(content), transport_version="fixture:v1")
    catalog_root = tmp_path / "inspection-input"
    source = write_shared_source_catalog(catalog_root, (
        SourceItem("document-a", "v1", (candidate,), metadata={"expectedSegments": 1}),
        SourceItem("document-b", "v1", (candidate,), metadata={"expectedSegments": 1}),
    ))
    values["workspace"] = LocalWorkspace(workspace.root, workspace.roots | {"sourceCatalog": catalog_root})
    values["plan"] = _replan(values["plan"], source_catalog=source, stages=stage_policy(stop_after="capture"),
                             processors=ProcessorSet(()))
    values["content_fetcher"] = SharedFixtureContentFetcher(workspace.roots["sourceContent"])
    return values


def _open(arguments, **references):
    return open_local_inspection(arguments["plan"], arguments["workspace"],
                                 document_release_producer=arguments["document_release_producer"], **references)


def _finish(arguments):
    with prepare_local_run(**arguments) as prepared:
        run = prepared.run()
        release = prepared.retain(run)
    return run, release


def test_live_plan_reports_pending_and_checkpointed_work_without_calling_it_a_result(arguments, monkeypatch):
    class Interrupted(BaseException):
        pass

    fetcher = arguments["content_fetcher"]
    original = fetcher.fetch
    calls = 0

    def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Interrupted()
        return original(*args, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", fetch)
    with prepare_local_run(**arguments) as prepared:
        before = _open(arguments).summary(sample_limit=0)
        assert before["phase"] == "plan-observation"
        assert before["run"] is None
        assert before["work"]["counts"]["scheduledItems"] == 2
        assert before["work"]["counts"]["capturedFiles"] == 0
        with pytest.raises(Interrupted):
            prepared.run()
        after = _open(arguments).summary()
        assert after["work"]["counts"]["capturedFiles"] == 1
        assert after["result"]["completeActiveState"] is False
        assert after["unavailable"]["wallClockRunDuration"]
        assert {row["stages"]["capture"]["status"] for row in after["work"]["sample"]} == {"pending", "recorded-complete"}
        assert all(row["stages"]["extraction"]["status"] == "not-requested" for row in after["work"]["sample"])


def test_later_processing_and_zero_work_result_keep_distinct_populations(arguments):
    _capture_run, capture = _finish(arguments)
    extractor = TextExtractor()
    processed_args = arguments | {"extractor": extractor}
    processed_args["plan"] = _replan(arguments["plan"], base_release=capture,
                                      stages=stage_policy(stop_after="extraction", extractor=extractor))
    processing_run, result = _finish(processed_args)
    view = _open(processed_args, run_ref=processing_run)
    counts = view.summary()["work"]["counts"]
    assert counts["newCapturedFiles"] == 0
    assert counts["reusedCapturedFiles"] == counts["newRepresentations"] == 2
    assert view.summary()["result"]["layers"]["files"] == 2
    detail = view.source("document-a")
    assert detail["work"]["stages"]["extraction"]["status"] == "recorded-complete"
    representation = detail["result"]["layers"]["representations"]["sample"][0]["payload"]
    blob = BlobRef.from_dict(representation["blob"])
    assert b"".join(view.read_blob(blob, max_bytes=blob.byte_size)) == b"Two retained documents."
    assert representation["evidenceMappings"]
    with pytest.raises(LimitExceededError):
        list(view.read_blob(blob, max_bytes=1))
    unchanged_args = processed_args | {"plan": _replan(processed_args["plan"], base_release=result)}
    unchanged_run, unchanged = _finish(unchanged_args)
    unchanged_view = _open(unchanged_args, release_ref=unchanged)
    summary = unchanged_view.summary()
    assert unchanged_run == unchanged_view.run_ref
    assert summary["work"]["counts"]["scheduledItems"] == 0
    assert summary["result"]["layers"]["files"] == 2
    assert unchanged_view.source("document-a")["work"] is None
    assert view.compare(unchanged_view)["result"]["changeCount"] == 0


def test_mixed_result_uses_per_source_stages_and_bounded_comparison(arguments):
    extractor = TextExtractor()
    full_args = arguments | {"extractor": extractor, "plan": _replan(arguments["plan"], stages=stage_policy(
        stop_after="extraction", extractor=extractor,
    ))}
    _, full = _finish(full_args)
    shortened_args = arguments | {"plan": _replan(arguments["plan"], base_release=full,
                                                  selection={"includeItemIds": ["document-a"]})}
    _, shortened = _finish(shortened_args)
    before, after = _open(full_args, release_ref=full), _open(shortened_args, release_ref=shortened)
    assert after.summary()["work"]["counts"]["scheduledItems"] == 1
    selected, inherited = after.source("document-a"), after.source("document-b")
    assert selected["result"]["layers"]["representations"]["count"] == 0
    assert inherited["work"] is None
    assert inherited["result"]["layers"]["representations"]["count"] == 1
    assert inherited["result"]["layers"]["dispositions"]["sample"][0]["payload"]["requestedStages"]["extractorId"]
    report = before.compare(after, sample_limit=0)
    assert report["result"]["changeCount"] == 1
    assert report["result"]["sample"] == []
    assert report["result"]["sampleTruncated"] is True
    detail = before.compare(after)["result"]["sample"][0]
    assert detail["sourceItemId"] == "document-a"
    assert detail["contentChanged"] and detail["configurationChanged"]
    assert detail["inputChanged"] is False


def test_same_content_with_new_delivery_is_a_provenance_change(arguments):
    _, base = _finish(arguments)
    next_args = arguments | {"plan": _replan(arguments["plan"], base_release=base,
        limits=replace(arguments["plan"].limits, max_processor_cost=200))}
    _, result = _finish(next_args)
    comparison = _open(arguments, release_ref=base).compare(_open(next_args, release_ref=result))
    assert comparison["result"]["changeCount"] == 2
    assert all(item["provenanceChanged"] and not item["contentChanged"] and not item["configurationChanged"]
               for item in comparison["result"]["sample"])
    assert any(change["path"] == "/limits/maxProcessorCost" for change in comparison["configurationChanges"])


def test_exact_run_never_uses_latest_and_refuses_changed_store_bytes(arguments, monkeypatch):
    run, _ = _finish(arguments)
    view = _open(arguments, run_ref=run)
    monkeypatch.setattr(view._stores, "latest", lambda _: pytest.fail("an exact run used mutable latest state"))
    assert view.summary()["work"]["counts"]["capturedFiles"] == 2
    assert view.source("document-a")["work"]
    reference, _store = next(view._work_stores())
    path = arguments["workspace"].roots["documentStores"] / reference.locator
    path.write_bytes(b"{}\n")
    with pytest.raises(IntegrityError, match="bytes differ"):
        view.summary()


def test_comparison_streams_each_result_layer_once_and_cleans_bounded_scratch(arguments, tmp_path, monkeypatch):
    _, release = _finish(arguments)
    old, new = _open(arguments, release_ref=release), _open(arguments, release_ref=release)
    observed = []
    for name, view in (("old", old), ("new", new)):
        original = view.records

        def records(kind, *, source_item_id=None, original=original, name=name):
            observed.append((name, kind))
            yield from original(kind, source_item_id=source_item_id)

        monkeypatch.setattr(view, "records", records)
    assert old.compare(new)["result"]["changeCount"] == 0
    assert len(observed) == len(set(observed)) == 2 * len(old.layer_kinds)
    scratch = tmp_path / "comparison-scratch"
    old._workspace_factory = LocalSqliteReconciliationWorkspaceFactory(scratch, max_spooled_bytes=1)
    with pytest.raises(LimitExceededError):
        old.compare(new)
    assert list(scratch.iterdir()) == []


def test_closing_a_live_record_stream_releases_the_work_iterator(arguments, monkeypatch):
    _finish(arguments)
    view = _open(arguments)
    original = view._work_stores
    closed = []

    def population():
        try:
            yield from original()
        finally:
            closed.append(True)

    monkeypatch.setattr(view, "_work_stores", population)
    rows = view.records("files")
    assert next(rows)["payload"]["blob"]
    rows.close()
    assert closed == [True]


@pytest.mark.parametrize("invalid_layers", ["missing-core", "orphaned-files"])
def test_resealed_run_cannot_claim_an_incomplete_active_result(arguments, invalid_layers):
    run_ref, _ = _finish(arguments)
    view = _open(arguments, run_ref=run_ref)
    run = view.run
    layers = ()
    if invalid_layers == "orphaned-files":
        sources = next(layer for layer in run.staged_layers if layer.layer_kind == "source-items")
        empty_sources = view._records.write_layer(
            (), layer_kind="source-items", schema=view._records.schema(sources),
            partition_policy=view._records.partition_policy(sources),
        )
        layers = tuple(empty_sources if layer == sources else layer for layer in run.staged_layers)
    values = {field.name: getattr(run, field.name) for field in fields(run) if field.name != "run_id"}
    invalid = RunReceipt.create(**(values | {"staged_layers": layers}))
    reference = view._controls.put(kind="run-receipts", artifact_id=invalid.run_id, value=invalid.to_dict())
    with pytest.raises(IntegrityError):
        _open(arguments, run_ref=reference)


def test_self_consistent_result_partitions_must_still_match_the_plan(arguments):
    run_ref, _ = _finish(arguments)
    view = _open(arguments, run_ref=run_ref)
    run = view.run
    policy = PartitionPolicy(run.partition_policy["policyId"], arguments["plan"].partition_count + 1)

    def repartition(layer):
        return view._records.write_layer(
            view._records.stream(layer), layer_kind=layer.layer_kind,
            schema=view._records.schema(layer), partition_policy=policy,
        )

    values = {field.name: getattr(run, field.name) for field in fields(run) if field.name != "run_id"}
    invalid = RunReceipt.create(**(values | {
        "store_ledger": repartition(run.store_ledger),
        "selection_ledger": repartition(run.selection_ledger),
        "task_result_ledger": repartition(run.task_result_ledger),
        "staged_layers": tuple(repartition(layer) for layer in run.staged_layers),
        "partition_policy": {"policyId": policy.policy_id, "bucketCount": policy.bucket_count},
    }))
    reference = view._controls.put(kind="run-receipts", artifact_id=invalid.run_id, value=invalid.to_dict())
    with pytest.raises(IntegrityError, match="partition count differs from its processing plan"):
        _open(arguments, run_ref=reference)


def test_rejected_run_explains_failure_without_claiming_complete_active_state(arguments, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("injected unsupported capture")

    monkeypatch.setattr(arguments["content_fetcher"], "fetch", broken)
    with prepare_local_run(**arguments) as prepared:
        run_ref = prepared.run()
    view = _open(arguments, run_ref=run_ref)
    summary = view.summary()
    assert summary["work"]["counts"]["failures"] == 2
    assert summary["result"]["scope"] == "rejected-staged-output"
    assert summary["result"]["completeActiveState"] is False
    assert view.source("document-a")["work"]["failures"]["sample"]
    assert view.compare(view)["result"] is None


def test_stateless_run_exposes_saved_work_without_complete_active_state(arguments):
    from docspec.application.reconcile import RunReconciler

    with prepare_local_run(**arguments) as prepared:
        results = tuple(prepared.execute_task(prepared.handoff, task) for task in prepared.task_source(prepared.handoff))
        composition = prepared._composition
        run_ref = RunReconciler(
            plan_ref=composition.plan_ref, execution_profile_ref=prepared.execution_profile_ref,
            execution_handoff_ref=prepared.handoff_ref, source_catalog_ref=arguments["plan"].source_catalog,
            base_release_ref=None, controls=composition.controls, stores=composition.stores,
            records=composition.records, document_catalog=composition.catalog,
            source_catalog=composition.source_catalog,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(arguments["workspace"].roots["reconciliation"]),
            partition_policy=composition.partition_policy, clock=composition.clock, stateful=False,
        ).reconcile_run(results)
    view = _open(arguments, run_ref=run_ref)
    summary = view.summary()
    assert summary["result"] == {"scope": "checkpointed-work", "completeActiveState": False, "layers": {}}
    assert len(list(view.records("files"))) == 2
    assert view.compare(view)["result"] is None


def test_comparison_detects_values_swapped_between_unchanged_inputs(tmp_path):
    from types import SimpleNamespace

    from docspec.application.inspection_comparison import _compare_rows, _result_signatures, _spool_result

    def rows(swapped):
        values = ("two", "one") if swapped else ("one", "two")
        for segment, value in zip(("segment-a", "segment-b"), values, strict=True):
            yield {"sourceItemId": "document-a", "recordId": f"{segment}-{value}",
                   "payload": {"inputIds": [segment], "schemaId": "example/1", "value": {"answer": value},
                               "disposition": "produced", "warnings": []}}

    old = SimpleNamespace(layer_kinds=("derived:processor",), records=lambda _: rows(False))
    new = SimpleNamespace(layer_kinds=("derived:processor",), records=lambda _: rows(True))
    with LocalSqliteReconciliationWorkspaceFactory(tmp_path).create() as workspace:
        _spool_result(old, workspace, "old")
        _spool_result(new, workspace, "new")
        comparison = _compare_rows(_result_signatures(workspace, "old"), _result_signatures(workspace, "new"), 1)
    assert comparison["changeCount"] == 1
    assert comparison["sample"][0]["contentChanged"] is True
