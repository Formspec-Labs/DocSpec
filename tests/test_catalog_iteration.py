"""Catalog previews and successive experiments preserve evidence and useful work."""

from dataclasses import dataclass
from types import SimpleNamespace
from itertools import count

import pytest
from rulespec_artifacts import Supersedes

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.catalog_preview import preview_catalog
from docspec.application.comparison import json_changes, paired_rows
from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.source_catalog import CatalogDisposition, CatalogSelectionDecision, SourceCatalogItem
from docspec.errors import IntegrityError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.runtime import CoreWorkspace, build_local_catalog, open_local_catalog, preview_local_catalog
from docspec.application.document_processors import content_statistics_processor
from docspec.application.documents import DocumentProcessor
from examples.dataset_example_support import document_results
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
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
    root = tmp_path / "experiment"
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "input.txt").write_bytes(_CONTENT)
    fetcher = LocalFileContentFetcher(inputs)
    fetches, extractions, segmentations = [], [], []
    extractor, segmenter = TextExtractor(), ParagraphSegmenter()
    for owner, method, calls in ((fetcher, "fetch", fetches), (extractor, "extract", extractions), (segmenter, "segment", segmentations)):
        original = getattr(owner, method)
        def counted(*args, _original=original, _calls=calls, **kwargs):
            _calls.append(True)
            return _original(*args, **kwargs)
        monkeypatch.setattr(owner, method, counted)

    def source(records, *, scope="complete-snapshot", namespace=_NAMESPACE):
        return SuppliedRecordSource(records, source_system_id=namespace, source_system_version="1",
            source_state_scope=scope, max_records=100, max_bytes=1024**2)

    def build(records, *, policy=None, scope="complete-snapshot", supersedes=None):
        return build_local_catalog((source(records, scope=scope),), root,
            policy=SuppliedRecordCatalogPolicy(_NAMESPACE, "1") if policy is None else policy,
            catalog_id="urn:test:iterative-catalog", producer=producer(),
            max_scratch_bytes=8 * 1024**2, supersedes=supersedes)

    with CoreWorkspace(root) as workspace:
        pipeline = workspace.documents(fetcher=fetcher, extractor=extractor, segmenter=segmenter)
        identities = count()
        def run(catalog, *, processors=(), fresh=False, stop_after=None):
            identity = str(next(identities))
            admitted = open_local_catalog(catalog.reference, root, producer=producer())
            pipeline.import_sources((SourceCatalogItem.from_dict(row) for row in admitted.iter_mappings()), state_id=identity + "-source")
            pipeline.run(identity + "-source", run_id=identity, dataset="documents", processors=processors,
                extract=stop_after != "capture", segment=stop_after not in {"capture", "extraction"}, fresh=fresh)
            return SimpleNamespace(state_id=identity, results=dict(document_results(workspace, pipeline, identity)),
                sources={key: value for key, _, value in pipeline.rows(identity + "-source")})
        yield SimpleNamespace(workspace=root, core=workspace, pipeline=pipeline, build=build, source=source, run=run,
            fetches=fetches, extractions=extractions, segmentations=segmentations)


def test_successor_policy_preview_and_growth_reuse_work_but_refresh_source_evidence(iteration):
    first = iteration.build([_record(name) for name in ("kept", "later-excluded", "removed")],
        policy=_ExcludeSuppliedPolicy(_NAMESPACE, "1"))
    processor = content_statistics_processor()
    older = iteration.run(first, processors=(processor,))
    successor = iteration.build([_record(name) for name in ("kept", "later-excluded", "added")],
        policy=_ExcludeSuppliedPolicy(_NAMESPACE, "1", ("later-excluded",)),
        supersedes=Supersedes(first.reference.catalog_id, first.reference.digest, "Grow and change selection"))
    snapshot = {path: path.stat().st_mtime_ns for path in iteration.workspace.rglob("*")}
    report = preview_local_catalog(successor.reference, iteration.workspace, producer=producer(), previous_ref=first.reference)
    assert {path: path.stat().st_mtime_ns for path in iteration.workspace.rglob("*")} == snapshot
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

    newer = iteration.run(successor, processors=(processor,))
    assert (len(iteration.fetches), len(iteration.extractions), len(iteration.segmentations)) == (4, 4, 4)
    kept_id = kept["sourceItemId"]
    assert newer.sources[kept_id]["metadata"]["sourceCatalogRow"] == kept
    assert newer.results[kept_id] == older.results[kept_id]
    assert len(newer.results) == 3
    assert sum(bool(results) for results in newer.results.values()) == 2
    assert iteration.core.ledger.current("documents") == ("state", newer.state_id)
    converged = iteration.run(successor, processors=(processor,))
    assert converged.results == newer.results
    assert len(iteration.fetches) == 4
    # Explicitly fresh work preserves source meaning while producing new attempts.
    clean = iteration.run(successor, processors=(processor,), fresh=True)
    assert clean.sources == newer.sources
    assert all(a.result_id != b.result_id for a, b in zip(clean.results[kept_id], newer.results[kept_id], strict=True))


@pytest.mark.parametrize("stop_after", ["capture", "extraction", "segmentation"])
def test_metadata_refresh_uses_the_deepest_requested_prefix(iteration, stop_after):
    first = iteration.build([_record("one")])
    older = iteration.run(first, stop_after=stop_after)
    calls = (len(iteration.fetches), len(iteration.extractions), len(iteration.segmentations))
    successor = iteration.build([_record("one", title="Updated catalog title")])
    newer = iteration.run(successor, stop_after=stop_after)
    assert (len(iteration.fetches), len(iteration.extractions), len(iteration.segmentations)) == calls
    assert next(iter(newer.sources.values()))["metadata"]["normalizedMetadata"]["title"] == "Updated catalog title"
    assert newer.results == older.results


def test_failed_processor_repairs_without_repeating_upstream_work(iteration):
    first = iteration.build([_record("one")])
    standard = content_statistics_processor()
    calls, broken = [], [True]
    def process(context, inputs):
        calls.append(True)
        if broken[0]:
            raise RuntimeError("processor unavailable")
        return standard.process(context, inputs)
    processor = DocumentProcessor(standard.name, standard.definition, process)
    with pytest.raises(RuntimeError, match="processor unavailable"):
        iteration.run(first, processors=(processor,))
    assert iteration.core.ledger.current("documents") is None
    broken[0] = False
    successor = iteration.build([_record("one", title="Updated title")])
    repaired = iteration.run(successor, processors=(processor,))
    assert len(calls) == 2
    assert (len(iteration.fetches), len(iteration.extractions), len(iteration.segmentations)) == (1, 1, 1)
    assert next(iter(repaired.sources.values()))["metadata"]["normalizedMetadata"]["title"] == "Updated title"


def test_capture_governing_limit_change_remains_material(iteration):
    first = iteration.build([_record("one")])
    older = iteration.run(first, stop_after="capture")
    iteration.pipeline.max_file_bytes += 1
    newer = iteration.run(first, stop_after="capture")
    assert len(iteration.fetches) == 2
    assert next(iter(older.results.values())) != next(iter(newer.results.values()))


def test_observed_crawl_omission_is_visible_and_does_not_mean_append(iteration):
    first = iteration.build([_record("one"), _record("two")])
    iteration.run(first, stop_after="capture")
    successor = iteration.build([_record("two")], scope="observed-crawl")
    report = preview_local_catalog(successor.reference, iteration.workspace, producer=producer(), previous_ref=first.reference)
    assert report["comparison"]["counts"] == {"added": 0, "removedFromCatalog": 1, "changed": 0, "unchanged": 1}
    assert "not evidence of publisher deletion" in report["interpretation"]["removedFromCatalog"]
    newer = iteration.run(successor, stop_after="capture")
    assert len(newer.results) == 1 and len(iteration.fetches) == 2


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
            preview_local_catalog(empty.reference, iteration.workspace / "absent", producer=producer(), **bounds)


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
