"""Catalog-only construction uses existing artifacts and honest supplied evidence."""

from dataclasses import replace

import pytest
from rulespec_artifacts import ArtifactVerificationError

from docspec.errors import IntegrityError, LimitExceededError
from docspec.runtime import build_local_catalog, open_local_catalog
from docspec.source_catalog import (
    LocalSourceCatalogStore, SourceCatalogBuilder, SourceCatalogBuildRequest,
    SourceCatalogCandidate, SqliteCatalogPolicyWorkspace,
    SuppliedRecordCatalogPolicy, SuppliedRecordSource,
)
from docspec.workspace import LocalWorkspace
from examples.supplied_records import demonstrate
from tests.support.source_catalog import producer


def _record(identity="one", *, candidates=True):
    return {
        "recordId": identity, "sourceIssuedVersion": "caller-revision-1", "title": "Café records",
        "metadata": {"raw": ["uninterpreted"], "topics": [{"label": "Not inferred as publisher evidence"}]},
        "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/plain", "immutable-object", "one.txt", expected_sha256=None, expected_byte_size=None,
        ).to_dict()] if candidates else [],
    }


def _source(records, *, namespace="urn:example:caller", scope="complete-snapshot", max_records=10, max_bytes=1024**2):
    return SuppliedRecordSource(records, source_system_id=namespace, source_system_version="1",
        source_state_scope=scope, max_records=max_records, max_bytes=max_bytes)


def _build(source, workspace, **overrides):
    settings = {
        "policy": SuppliedRecordCatalogPolicy(source.describe().source_system_id, source.describe().source_system_version),
        "catalog_id": "urn:example:catalog", "producer": producer(), "max_scratch_bytes": 8 * 1024**2,
    }
    return build_local_catalog((source,), workspace, **(settings | overrides))


def test_supplied_snapshot_is_order_independent_and_isolated_from_caller_mutation():
    raw = [_record("z"), _record("a")]
    snapshot = _source(raw)
    same = _source(reversed(raw))
    assert snapshot.describe() == same.describe()
    original = tuple(snapshot.iter_records())
    raw[0]["metadata"]["raw"].append("changed after snapshot")
    original[0]["record"]["title"] = "Changed returned dictionary"
    assert tuple(snapshot.iter_records()) == tuple(same.iter_records())
    assert _source(raw).describe() != snapshot.describe()
    assert _source([_record("z"), _record("a")], namespace="urn:example:other").describe() != snapshot.describe()
    assert _source([_record("z"), _record("a")], scope="observed-crawl").describe() != snapshot.describe()
    ids = [row["sourceRecordId"] for row in snapshot.iter_records()]
    assert ids == sorted(ids, key=lambda value: value.encode("utf-16-be"))


def test_catalog_only_build_matches_explicit_assembly_and_keeps_source_facts(tmp_path):
    source = _source([_record(), _record("without-body", candidates=False)])
    workspace = LocalWorkspace(tmp_path / "convenient")
    result = _build(source, workspace)
    explicit = SourceCatalogBuilder(
        store=LocalSourceCatalogStore(tmp_path / "explicit"),
        policy=SuppliedRecordCatalogPolicy("urn:example:caller", "1"),
        request=SourceCatalogBuildRequest("urn:example:catalog", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    assert result.reference == explicit.reference
    assert {path.name for path in workspace.root.iterdir()} == {"sourceCatalog"}
    before = {path: path.stat().st_mtime_ns for path in workspace.root.rglob("*")}
    admitted = open_local_catalog(result.reference, workspace, producer=producer())
    rows = tuple(admitted.iter_mappings())
    assert admitted.summary.item_count == 2
    assert admitted.summary.disposition_counts == {"selected": 1, "excluded": 0, "deleted": 0, "unavailable": 1, "failed": 0}
    selected = next(row for row in rows if row["selection"]["disposition"] == "selected")
    assert selected["sourceNativeFacts"][0]["fields"] == _record()
    assert selected["normalizedMetadata"]["title"] == "Café records"
    assert selected["normalizedMetadata"]["publicationDate"] is None
    assert selected["sourceObservedTopics"] == []
    assert selected["sourceObservations"] == [{"observationKey": "docspec.input-origin", "observationValue": "caller-supplied-records"}]
    assert next(row for row in rows if row["selection"]["disposition"] == "unavailable")["selection"]["reasonCode"] == "supplied.no-candidate"
    assert {path: path.stat().st_mtime_ns for path in workspace.root.rglob("*")} == before


def test_empty_supplied_snapshot_is_an_explicit_empty_catalog(tmp_path):
    source = _source([])
    workspace = LocalWorkspace(tmp_path / "empty")
    result = _build(source, workspace)
    assert result.summary.item_count == 0
    assert tuple(open_local_catalog(result.reference, workspace, producer=producer()).iter_mappings()) == ()


def test_supplied_record_example_uses_only_catalog_storage(tmp_path):
    output = tmp_path / "example"
    result = demonstrate(output)
    assert result["itemCount"] == 2
    assert {row["selection"]["disposition"] for row in result["items"]} == {"selected", "unavailable"}
    assert {path.name for path in output.iterdir()} == {"sourceCatalog"}


def test_source_namespaces_prevent_equal_local_record_ids_from_colliding(tmp_path):
    first, second = _source([_record()]), _source([_record()], namespace="urn:example:other")
    first_workspace, second_workspace = LocalWorkspace(tmp_path / "first"), LocalWorkspace(tmp_path / "second")
    left, right = _build(first, first_workspace), _build(second, second_workspace)
    left_row = tuple(open_local_catalog(left.reference, first_workspace, producer=producer()).iter_mappings())[0]
    right_row = tuple(open_local_catalog(right.reference, second_workspace, producer=producer()).iter_mappings())[0]
    assert left_row["documentId"] == right_row["documentId"] == "one"
    assert left_row["sourceItemId"] != right_row["sourceItemId"]
    assert left.reference != right.reference


def test_supplied_policy_refuses_another_source_namespace(tmp_path):
    with pytest.raises(IntegrityError, match="matched no source-native input"):
        _build(_source([_record()]), LocalWorkspace(tmp_path / "dataset"),
            policy=SuppliedRecordCatalogPolicy("urn:example:unavailable-source", "1"))


def test_catalog_open_requires_exact_ref_and_independent_producer(tmp_path):
    source = _source([_record()])
    workspace = LocalWorkspace(tmp_path / "dataset")
    result = _build(source, workspace)
    with pytest.raises(IntegrityError):
        open_local_catalog(result.reference, workspace, producer=replace(producer(), implementation_id="another"))
    with pytest.raises((IntegrityError, FileNotFoundError)):
        open_local_catalog(replace(result.reference, digest="sha256:" + "0" * 64), workspace, producer=producer())
    missing = LocalWorkspace(tmp_path / "missing")
    with pytest.raises(ValueError, match="root is missing"):
        open_local_catalog(result.reference, missing, producer=producer())
    assert not missing.root.exists()


@pytest.mark.parametrize("invalid", ["count", "bytes", "duplicate", "unknown-field", "duplicate-candidate", "bad-scope"])
def test_invalid_supplied_snapshot_refuses_and_closes_input(invalid):
    records, options = [_record(), _record("two")], {}
    if invalid == "count":
        options["max_records"] = 1
    elif invalid == "bytes":
        options["max_bytes"] = 1
    elif invalid == "duplicate":
        records[1] = _record()
    elif invalid == "unknown-field":
        records[0]["observedCollectionSucceeded"] = True
    elif invalid == "duplicate-candidate":
        records[0]["candidateRenditions"] *= 2
    else:
        options["scope"] = "invented"
    closed = []

    def stream():
        try:
            yield from records
        finally:
            closed.append(True)

    with pytest.raises((ValueError, LimitExceededError)):
        _source(stream(), **options)
    if invalid != "bad-scope":
        assert closed == [True]


def test_bad_catalog_scratch_limit_refuses_before_creating_source_storage(tmp_path):
    workspace = LocalWorkspace(tmp_path / "dataset")
    with pytest.raises(LimitExceededError, match="minimum SQLite"):
        _build(_source([_record()]), workspace, max_scratch_bytes=1)
    assert not workspace.root.exists()


def test_invalid_output_producer_refuses_before_creating_catalog_or_resume_state(tmp_path):
    workspace = LocalWorkspace(tmp_path / "dataset")
    resume = tmp_path / "scratch" / "resume.sqlite3"
    invalid = replace(producer(), implementation_id="not-an-absolute-implementation-id")
    with pytest.raises(ArtifactVerificationError, match="absolute identifier"):
        _build(_source([_record()]), workspace, producer=invalid, resume_workspace=resume)
    assert not workspace.root.exists()
    assert not resume.parent.exists()


def test_catalog_recovery_uses_existing_durable_builder_state(tmp_path):
    source = _source([_record()])
    workspace = LocalWorkspace(tmp_path / "dataset")
    resume = tmp_path / "catalog-resume.sqlite3"
    failure = RuntimeError("stop after source rows are staged")
    policy = SuppliedRecordCatalogPolicy("urn:example:caller", "1")

    class StopOnce:
        policy_id, policy_version = policy.policy_id, policy.policy_version
        configuration, universe_inputs = policy.configuration, policy.universe_inputs

        def iter_items(self, inputs, scratch):
            tuple(inputs.iter_universe_rows())
            raise failure
            yield

    with pytest.raises(RuntimeError, match="stop after source rows"):
        _build(source, workspace, policy=StopOnce(), resume_workspace=resume)
    assert resume.exists()
    resumed = _build(source, workspace, resume_workspace=resume)
    clean = _build(source, LocalWorkspace(tmp_path / "clean"))
    assert resumed.reference == clean.reference
