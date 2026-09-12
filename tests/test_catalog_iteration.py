"""Catalog previews and successive experiments preserve evidence and useful work."""

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from rulespec_artifacts import Supersedes

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.catalog_preview import preview_catalog
from docspec.application.comparison import json_changes, paired_rows
from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.jobs import EntryExecutionMode, FailureClass
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.source_catalog import CatalogDisposition, CatalogSelectionDecision, SourceCatalogItem
from docspec.errors import IntegrityError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.runtime import build_local_catalog, open_local_catalog, open_local_inspection, prepare_local_experiment, preview_local_catalog
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from tests.support.experiments import _FailingProcessor
from tests.support.processors import _CountingExtractor, _CountingProcessor, _CountingSegmenter, _description
from tests.support.source_catalog import producer

_NAMESPACE = "urn:test:catalog-iteration"
_CONTENT = b"First paragraph.\n\nSecond paragraph."


def _record(identifier, *, title="Source title", candidates=True):
    return {
        "recordId": identifier, "sourceIssuedVersion": "v1", "title": title, "metadata": {},
        "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/plain", "immutable-object", "input.txt",
            expected_sha256=sha256_digest(_CONTENT), expected_byte_size=len(_CONTENT),
        ).to_dict()] if candidates else [],
    }


@dataclass(frozen=True)
class _ExcludeSuppliedPolicy(SuppliedRecordCatalogPolicy):
    """Exercise the existing policy port with one explicit dataset decision."""

    excluded_ids: tuple[str, ...] = ()
    policy_id = "urn:docspec:test:catalog-policy:exclude-supplied"

    @property
    def configuration(self):
        return dict(super().configuration) | {"excludedRecordIds": list(self.excluded_ids)}

    def iter_items(self, inputs, workspace):
        for item in super().iter_items(inputs, workspace):
            if item.document_id not in self.excluded_ids:
                yield item
                continue
            row = item.to_dict()
            selection = {"disposition": "excluded", "reasonCode": "dataset.excluded", "reason": "Excluded by the dataset's chosen record list."}
            row["selection"] = selection
            result = next(value["result"] for value in row["interpretations"] if value["interpretationKind"] == "selection")
            result.update(finalDisposition="excluded", reasonCode=selection["reasonCode"], reason=selection["reason"])
            result["decisions"].append(CatalogSelectionDecision(
                "dataset-record-list", False, CatalogDisposition.EXCLUDED,
                selection["reasonCode"], selection["reason"],
            ).to_dict())
            yield SourceCatalogItem.from_dict(row)


@pytest.fixture
def iteration(tmp_path, monkeypatch):
    workspace = LocalWorkspace(tmp_path / "experiment")
    workspace.roots["sourceContent"].mkdir(parents=True)
    (workspace.roots["sourceContent"] / "input.txt").write_bytes(_CONTENT)
    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0)
    fetcher = LocalFileContentFetcher(workspace.roots["sourceContent"])
    fetches = []
    fetch = fetcher.fetch

    def counted(*args, **kwargs):
        fetches.append(args[0].candidate_id)
        return fetch(*args, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", counted)
    extractor, segmenter = _CountingExtractor(TextExtractor()), _CountingSegmenter(ParagraphSegmenter())
    limits = WorkLimits(10, 1024**2, 100, 100, 100, 16 * 1024**2, 60, max_attempts=1)

    def source(records, *, scope="complete-snapshot", namespace=_NAMESPACE):
        return SuppliedRecordSource(records, source_system_id=namespace, source_system_version="1",
            source_state_scope=scope, max_records=100, max_bytes=1024**2)

    def build(records, *, policy=None, scope="complete-snapshot", supersedes=None):
        return build_local_catalog((source(records, scope=scope),), workspace,
            policy=SuppliedRecordCatalogPolicy(_NAMESPACE, "1") if policy is None else policy,
            catalog_id="urn:test:iterative-catalog", producer=producer(),
            max_scratch_bytes=8 * 1024**2, supersedes=supersedes)

    def run(catalog, *, processors=(), fresh=False, **choices):
        chosen_workspace = LocalWorkspace(workspace.root / "fresh", {
            "sourceCatalog": workspace.roots["sourceCatalog"], "sourceContent": workspace.roots["sourceContent"],
        }) if fresh else workspace
        defaults = {
            "limits": limits, "retry_policy": retry,
            "accepted_failure_policy": AcceptedFailurePolicy(accepted_classes=(FailureClass.DETERMINISTIC_INPUT,)),
            "source_catalog_producer": producer(), "document_release_producer": producer(),
            "completed_at": "2026-09-11T00:00:00Z", "deadline_epoch_seconds": 4102444800,
            "content_fetcher": fetcher, "extractor": extractor, "segmenter": segmenter,
        }
        with prepare_local_experiment(catalog.reference, chosen_workspace, processors=processors, **(defaults | choices)) as prepared:
            entries = tuple(entry for task in prepared.task_source(prepared.handoff)
                for entry in prepared._composition.stores.load(task.input_store).entries)
            before = open_local_inspection(prepared.plan, chosen_workspace, document_release_producer=producer()).summary()
            retained = prepared.retain(prepared.run())
            view = open_local_inspection(prepared.plan, chosen_workspace,
                document_release_producer=producer(), release_ref=retained)
            return retained, view, entries, before

    return SimpleNamespace(workspace=workspace, build=build, source=source, run=run, retry=retry,
        limits=limits, fetches=fetches, extractor=extractor, segmenter=segmenter)


def _payloads(view, kind):
    return tuple(row["payload"] for row in view.records(kind))


def test_successor_policy_preview_and_growth_reuse_work_but_refresh_source_evidence(iteration):
    first = iteration.build([_record(name) for name in ("kept", "later-excluded", "removed")],
        policy=_ExcludeSuppliedPolicy(_NAMESPACE, "1"))
    processor = _CountingProcessor(_description("record", "1", iteration.retry))
    base, older, _, _ = iteration.run(first, processors=(processor,))
    successor = iteration.build([_record(name) for name in ("kept", "later-excluded", "added")],
        policy=_ExcludeSuppliedPolicy(_NAMESPACE, "1", ("later-excluded",)),
        supersedes=Supersedes(first.reference.catalog_id, first.reference.digest, "Grow and change selection"))
    snapshot = {path: path.stat().st_mtime_ns for path in iteration.workspace.root.rglob("*")}
    report = preview_local_catalog(successor.reference, iteration.workspace, producer=producer(), previous_ref=first.reference)
    assert {path: path.stat().st_mtime_ns for path in iteration.workspace.root.rglob("*")} == snapshot
    assert len(iteration.fetches) == 3
    assert report["comparison"]["counts"] == {"added": 1, "removedFromCatalog": 1, "changed": 2, "unchanged": 0}
    assert report["catalog"]["catalogSelection"]["counts"]["selected"] == 2
    excluded = next(row for row in report["sample"] if row["documentId"] == "later-excluded")
    assert excluded["selection"]["reasonCode"] == "dataset.excluded"
    kept = next(row for row in report["sample"] if row["documentId"] == "kept")
    change = next(row for row in report["comparison"]["sample"] if row["sourceItemId"] == kept["sourceItemId"])
    assert change["changedFields"] == ["interpretations"]
    assert report["comparison"]["selectionPolicyChanges"]
    assert report["catalog"]["supersedes"]["artifactDigest"] == first.reference.digest

    retained, newer, entries, before = iteration.run(successor, processors=(processor,), base_release=base)
    kept_entry = next(entry for entry in entries if entry.source_item.item_id == kept["sourceItemId"])
    assert kept_entry.execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert kept_entry.processor_ids_to_run == ()
    assert before["work"]["counts"]["scheduledItems"] == 4
    assert (len(iteration.fetches), iteration.extractor.calls, iteration.segmenter.calls, len(processor.calls)) == (4, 4, 4, 8)
    kept_source = next(row for row in _payloads(newer, "source-items") if row["itemId"] == kept["sourceItemId"])
    assert kept_source["metadata"]["sourceCatalogRow"] == kept
    for kind in ("files", "representations", "segments", f"derived:{processor.description.processor_id}"):
        assert tuple(row for row in _payloads(newer, kind) if row["sourceItemId"] == kept["sourceItemId"]) == tuple(
            row for row in _payloads(older, kind) if row["sourceItemId"] == kept["sourceItemId"])
    # A clean run uses the same source files and implementation pins; retained
    # live inputs/values agree. Only the incremental result has a tombstone for
    # the item explicitly removed from its predecessor.
    _, clean, _, _ = iteration.run(successor, processors=(processor,), fresh=True)
    current_ids = {row["sourceItemId"] for row in report["sample"]}
    for item in newer.compare(clean)["result"]["sample"]:
        if item["sourceItemId"] in current_ids:
            assert not item["inputChanged"] and not item["contentChanged"] and not item["configurationChanged"]
    _, converged, entries, _ = iteration.run(successor, processors=(processor,), base_release=retained)
    assert entries == ()
    assert converged.compare(newer)["result"]["changeCount"] == 0


@pytest.mark.parametrize("stop_after, mode", [
    ("capture", EntryExecutionMode.FROM_CAPTURES),
    ("extraction", EntryExecutionMode.FROM_REPRESENTATIONS),
    ("segmentation", EntryExecutionMode.FROM_SEGMENTS),
])
def test_metadata_refresh_uses_the_deepest_requested_prefix(iteration, stop_after, mode):
    choices = {"stop_after": stop_after}
    if stop_after == "capture":
        choices["extractor"] = choices["segmenter"] = None
    elif stop_after == "extraction":
        choices["segmenter"] = None
    first = iteration.build([_record("one")])
    base, older, _, _ = iteration.run(first, **choices)
    calls = (len(iteration.fetches), iteration.extractor.calls, iteration.segmenter.calls)
    successor = iteration.build([_record("one", title="Updated catalog title")])
    _, newer, entries, _ = iteration.run(successor, base_release=base, **choices)
    assert entries[0].execution_mode is mode
    assert (len(iteration.fetches), iteration.extractor.calls, iteration.segmenter.calls) == calls
    assert _payloads(newer, "source-items")[0]["metadata"]["normalizedMetadata"]["title"] == "Updated catalog title"
    assert _payloads(newer, "files") == _payloads(older, "files")


def test_policy_only_change_holds_permanent_failure_until_explicit_repair(iteration):
    first = iteration.build([_record("one")], policy=_ExcludeSuppliedPolicy(_NAMESPACE, "1"))
    failing = _FailingProcessor(_description("fail", "1", iteration.retry))
    base, failed, _, _ = iteration.run(first, processors=(failing,))
    successor = iteration.build([_record("one")], policy=_ExcludeSuppliedPolicy(_NAMESPACE, "1", ("another-record",)))
    failing.fail = False
    held_base, held, entries, _ = iteration.run(successor, processors=(failing,), base_release=base)
    assert entries == () and failing.attempts == 1
    assert _payloads(held, "source-items") == _payloads(failed, "source-items")
    assert _payloads(held, "dispositions") == _payloads(failed, "dispositions")
    _, repaired, entries, _ = iteration.run(successor, processors=(failing,), base_release=held_base,
        selection={"retryFailures": "selected"})
    assert entries[0].execution_mode is EntryExecutionMode.FROM_SEGMENTS
    assert (len(iteration.fetches), iteration.extractor.calls, iteration.segmenter.calls) == (1, 1, 1)
    assert _payloads(repaired, "source-items") != _payloads(held, "source-items")
    assert _payloads(repaired, "dispositions")[0]["terminalFailure"] is None


def test_metadata_refresh_keeps_non_stage_governing_changes_conservative(iteration):
    first = iteration.build([_record("one")])
    base, _, _, _ = iteration.run(first)
    successor = iteration.build([_record("one", title="Updated title")])
    _, _, entries, _ = iteration.run(successor, base_release=base,
        limits=replace(iteration.limits, max_processor_cost=iteration.limits.max_processor_cost + 1))
    assert entries[0].execution_mode is EntryExecutionMode.FULL
    assert (len(iteration.fetches), iteration.extractor.calls, iteration.segmenter.calls) == (2, 2, 2)


def test_observed_crawl_omission_is_visible_and_does_not_mean_append(iteration):
    first = iteration.build([_record("one"), _record("two")])
    base, _, _, _ = iteration.run(first, stop_after="capture", extractor=None, segmenter=None)
    successor = iteration.build([_record("two")], scope="observed-crawl")
    report = preview_local_catalog(successor.reference, iteration.workspace, producer=producer(), previous_ref=first.reference)
    assert report["comparison"]["counts"] == {"added": 0, "removedFromCatalog": 1, "changed": 0, "unchanged": 1}
    assert "not evidence of publisher deletion" in report["interpretation"]["removedFromCatalog"]
    _, newer, _, _ = iteration.run(successor, base_release=base, stop_after="capture", extractor=None, segmenter=None)
    live_files = _payloads(newer, "files")
    assert len(live_files) == 1 and len(iteration.fetches) == 2


def test_disjoint_single_policy_inputs_grow_and_duplicate_qualified_ids_refuse(iteration):
    inputs = (iteration.source([_record("one")]), iteration.source([_record("two")]))
    settings = {"policy": SuppliedRecordCatalogPolicy(_NAMESPACE, "1"), "catalog_id": "urn:test:multiple-parts",
        "producer": producer(), "max_scratch_bytes": 8 * 1024**2}
    built = build_local_catalog(inputs, iteration.workspace, **settings)
    assert built.summary.item_count == 2
    # Distinct source pins can still repeat a qualified ID; it must not pick a winner.
    repeated = iteration.source([_record("one", title="Another version of the same record")])
    with pytest.raises(IntegrityError, match="duplicate|repeat|collision"):
        build_local_catalog((inputs[0], repeated), iteration.workspace, **settings)


@pytest.mark.parametrize("limit, byte_limit", [(0, 1024**2), (20, 0), (20, 1), (1, 1024**2)])
def test_preview_sample_bounds_keep_complete_counts(iteration, limit, byte_limit):
    first = iteration.build([_record("one"), _record("two")])
    successor = iteration.build([_record("one", title="Changed"), _record("three", candidates=False)])
    report = preview_local_catalog(successor.reference, iteration.workspace, producer=producer(),
        previous_ref=first.reference, sample_limit=limit, max_sample_bytes=byte_limit)
    assert report["catalog"]["itemCount"] == 2
    assert report["comparison"]["changeCount"] == 3
    for section in (report, report["comparison"]):
        assert len(section["sample"]) <= limit
        assert section["sampleBytes"] == sum(len(canonical_json_bytes(row)) for row in section["sample"])
        assert section["sampleBytes"] <= byte_limit
        assert section["sampleTruncated"]
    assert iteration.fetches == []


def test_empty_catalog_preview_and_invalid_bounds(iteration):
    empty = iteration.build([])
    report = preview_local_catalog(empty.reference, iteration.workspace, producer=producer(), previous_ref=empty.reference)
    assert report["sample"] == [] and report["sampleTruncated"] is False
    assert report["comparison"]["changeCount"] == 0
    for bounds in ({"sample_limit": True}, {"sample_limit": -1}, {"max_sample_bytes": -1}):
        with pytest.raises(ValueError, match="non-negative integer"):
            preview_local_catalog(empty.reference, LocalWorkspace(iteration.workspace.root / "absent"), producer=producer(), **bounds)


def test_preview_exhausts_and_closes_admitted_rows_with_zero_samples(iteration):
    built = iteration.build([_record("one")])
    admitted = open_local_catalog(built.reference, iteration.workspace, producer=producer())
    closed = []

    def damaged():
        try:
            yield from admitted.iter_mappings()
            raise IntegrityError("late row verification failed")
        finally:
            closed.append(True)

    iterator = damaged()
    with pytest.raises(IntegrityError, match="late row verification"):
        preview_catalog(admitted.summary, iterator, previous_summary=None, previous_rows=iter(()), sample_limit=0, max_sample_bytes=0)
    assert closed == [True]


def test_catalog_merge_uses_utf16_order_and_closes_both_inputs_on_early_exit():
    # U+10000 sorts before U+E000 in catalog order, unlike Python string order.
    closed = []

    def rows(name, identities):
        try:
            yield from ({"sourceItemId": value} for value in identities)
        finally:
            closed.append(name)

    old = rows("old", ["\U00010000", "\ue000"])
    new = rows("new", ["\ue000"])
    pairs = paired_rows(old, new, key=lambda row: row["sourceItemId"].encode("utf-16-be"))
    assert next(pairs) == ({"sourceItemId": "\U00010000"}, None)
    pairs.close()
    assert sorted(closed) == ["new", "old"]


def test_shared_field_comparison_distinguishes_absent_and_null():
    assert json_changes({"settings": {}}, {"settings": {"optional": None}}) == [{
        "path": "/settings/optional", "older": None, "newer": None,
        "olderPresent": False, "newerPresent": True,
    }]
    assert json_changes({"settings": {"optional": None}}, {"settings": {}})[0]["newerPresent"] is False
    assert json_changes({"a/b~c": 1}, {"a/b~c": 2})[0]["path"] == "/a~1b~0c"


def test_public_preview_distinguishes_boolean_and_numeric_source_facts(iteration):
    record = _record("one")
    first = iteration.build([{**record, "metadata": {"nested": {"choice": 1}}}])
    successor = iteration.build([{**record, "metadata": {"nested": {"choice": True}}}])
    report = preview_local_catalog(successor.reference, iteration.workspace, producer=producer(), previous_ref=first.reference)
    assert report["comparison"]["counts"]["changed"] == 1
    change = report["comparison"]["sample"][0]
    assert "sourceNativeFacts" in change["changedFields"]
    difference = next(value for value in change["differences"] if value["path"] == "/sourceNativeFacts")
    assert type(difference["older"][0]["fields"]["metadata"]["nested"]["choice"]) is int
    assert difference["newer"][0]["fields"]["metadata"]["nested"]["choice"] is True
    assert json_changes({"choice": 1}, {"choice": True}) == [{"path": "/choice", "older": 1, "newer": True}]
