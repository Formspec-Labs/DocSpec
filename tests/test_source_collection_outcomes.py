"""Collection acceptance preserves reported evidence without inventing work."""

from dataclasses import replace

import pytest

from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.plans import WorkLimits
from docspec.domain.source_outcomes import DEFAULT_ACCEPTED_RECORD_OUTCOMES
from docspec.errors import IntegrityError, LimitExceededError
from docspec.runtime import build_local_catalog, open_local_catalog, open_local_inspection, prepare_local_experiment, preview_local_catalog
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from tests.support.source_catalog import FakeSource, _FEDERAL_REGISTER_SOURCE, description, producer, record, renditions


def _source(outcome="no-record-rejections", *, count=1):
    reported = None if outcome is None else {
        "recordOutcome": outcome, "sourceStateScope": "complete-snapshot",
        "requestedScope": {"query": {"page": 1}}, "warnings": ["Provider-reported fixture"],
        "publishedRecordCount": count, "failedRecordCount": int(outcome in {"partial-rejection", "total-rejection"}),
    }
    ids = tuple(f"2026-{index:05d}" for index in range(count))
    return FakeSource(replace(description(), collection_outcome=reported),
        tuple(record(identity) for identity in ids), tuple(value for identity in ids for value in renditions(identity)))


def _build(source, workspace, **options):
    settings = {
        "policy": FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        "catalog_id": "urn:test:collection-outcomes", "producer": producer(), "max_scratch_bytes": 8 * 1024**2,
    }
    return build_local_catalog((source,), workspace, **(settings | options))


@pytest.mark.parametrize("outcome,count", [("partial-rejection", 1), ("total-rejection", 0)])
def test_rejected_input_refuses_before_source_iteration_or_workspace_creation(tmp_path, outcome, count):
    source = _source(outcome, count=count)

    def unexpected():
        raise AssertionError("refused input was consumed")

    source.iter_records = unexpected
    workspace = LocalWorkspace(tmp_path / "output")
    with pytest.raises(ValueError, match=f"{outcome!r} is not accepted"):
        _build(source, workspace)
    assert not workspace.root.exists()


def test_partial_acceptance_does_not_authorize_total_rejection(tmp_path):
    accepted = DEFAULT_ACCEPTED_RECORD_OUTCOMES | {"partial-rejection"}
    partial = _build(_source("partial-rejection"), LocalWorkspace(tmp_path / "partial"), accepted_record_outcomes=accepted)
    assert partial.summary.item_count == 1
    total = LocalWorkspace(tmp_path / "total")
    with pytest.raises(ValueError, match="total-rejection"):
        _build(_source("total-rejection", count=0), total, accepted_record_outcomes=accepted)
    assert not total.root.exists()


@pytest.mark.parametrize("outcome,count", [(None, 0), ("empty", 0), ("total-rejection", 0), ("no-record-rejections", 1)])
def test_reopened_catalog_distinguishes_unreported_empty_rejected_and_successful_input(tmp_path, outcome, count):
    source, workspace = _source(outcome, count=count), LocalWorkspace(tmp_path / "catalog")
    accepted = DEFAULT_ACCEPTED_RECORD_OUTCOMES | {"total-rejection"}
    result = _build(source, workspace, accepted_record_outcomes=accepted)
    admitted = open_local_catalog(result.reference, workspace, producer=producer())
    preview = preview_local_catalog(result.reference, workspace, producer=producer())
    assert canonical_json_bytes(admitted.summary.source_native_inputs[0]) == canonical_json_bytes(source.describe().to_dict())
    assert preview["catalog"]["sourceNativeInputs"][0] == source.describe().to_dict()
    assert preview["catalog"]["acceptedRecordOutcomes"] == sorted(accepted)
    assert admitted.summary.disposition_counts["failed"] == 0
    assert admitted.summary.item_count == count


def test_description_is_deeply_snapshotted_and_read_once_for_build(tmp_path):
    source = _source("partial-rejection")
    original = source.describe()
    report = original.to_dict()
    report["collectionOutcome"]["requestedScope"]["query"]["page"] = True
    original_page = original.to_dict()["collectionOutcome"]["requestedScope"]["query"]["page"]
    assert type(original_page) is int and original_page == 1
    calls = []

    def describe_once():
        calls.append(None)
        if len(calls) > 1:
            raise AssertionError("provider description read twice")
        return original

    source.describe = describe_once
    result = _build(source, LocalWorkspace(tmp_path / "catalog"),
        accepted_record_outcomes=DEFAULT_ACCEPTED_RECORD_OUTCOMES | {"partial-rejection"})
    assert len(calls) == 1
    assert canonical_json_bytes(result.summary.source_native_inputs[0]) == canonical_json_bytes(original.to_dict())


@pytest.mark.parametrize("change", ["acceptance", "description"])
def test_resume_refuses_changed_acceptance_or_reported_description(tmp_path, change):
    source, workspace = _source(), LocalWorkspace(tmp_path / "catalog")
    resume = tmp_path / "resume.sqlite"
    original = source.metadata
    policy = FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE)

    class StopOnce:
        policy_id, policy_version = policy.policy_id, policy.policy_version
        configuration, universe_inputs = policy.configuration, policy.universe_inputs

        def iter_items(self, inputs, scratch):
            tuple(inputs.iter_universe_rows())
            raise RuntimeError("interrupted after source staging")
            yield

    with pytest.raises(RuntimeError, match="interrupted"):
        _build(source, workspace, policy=StopOnce(), resume_workspace=resume)
    options = {}
    if change == "acceptance":
        options["accepted_record_outcomes"] = DEFAULT_ACCEPTED_RECORD_OUTCOMES | {"partial-rejection"}
    else:
        changed = source.metadata.to_dict()["collectionOutcome"]
        changed["requestedScope"]["query"]["page"] = True
        source.metadata = replace(source.metadata, collection_outcome=changed)
    with pytest.raises(IntegrityError, match="different build"):
        _build(source, workspace, resume_workspace=resume, **options)
    source.metadata = original
    result = _build(source, workspace, resume_workspace=resume)
    assert result.reference == _build(source, LocalWorkspace(tmp_path / "clean")).reference


def test_oversized_reported_evidence_refuses_without_truncation():
    with pytest.raises(LimitExceededError, match="metadata byte limit"):
        replace(description(), collection_outcome={
            "recordOutcome": "empty", "sourceStateScope": "complete-snapshot", "warnings": ["x" * 1024**2],
        })


def test_catalog_receipt_overflow_refuses_instead_of_omitting_descriptions(tmp_path, monkeypatch):
    from docspec.adapters.catalog_artifact import builder

    monkeypatch.setattr(builder, "MAX_SMALL_MEMBER_BYTES", 2048)
    workspace = LocalWorkspace(tmp_path / "catalog")
    with pytest.raises(LimitExceededError, match="receipt exceeds"):
        _build(_source(), workspace)
    assert not tuple(workspace.roots["sourceCatalog"].rglob("release.json"))


def test_accepted_partial_successor_is_still_a_full_dataset_universe(tmp_path):
    workspace = LocalWorkspace(tmp_path / "dataset")
    workspace.roots["sourceContent"].mkdir(parents=True)
    (workspace.roots["sourceContent"] / "input.txt").write_bytes(b"Source content.")
    namespace = "urn:test:reported-input"

    def catalog(ids, outcome):
        supplied = SuppliedRecordSource(tuple({
            "recordId": identity, "sourceIssuedVersion": "1", "title": identity, "metadata": {},
            "candidateRenditions": [SourceCatalogCandidate("body", "text/plain", "immutable-object", "input.txt").to_dict()],
        } for identity in ids), source_system_id=namespace, source_system_version="1",
            source_state_scope="observed-crawl", max_records=10, max_bytes=1024**2)
        # This test source explicitly reports a collection outcome; the ordinary
        # SuppliedRecordSource continues to make no such observation itself.
        source = FakeSource(replace(supplied.describe(), collection_outcome={
            "recordOutcome": outcome, "sourceStateScope": "observed-crawl", "publishedRecordCount": len(ids),
        }), tuple(supplied.iter_records()), tuple(supplied.iter_renditions()))
        return build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
            catalog_id="urn:test:reported-input:catalog", producer=producer(), max_scratch_bytes=8 * 1024**2,
            accepted_record_outcomes=DEFAULT_ACCEPTED_RECORD_OUTCOMES | {"partial-rejection"})

    settings = {
        "stop_after": "capture", "limits": WorkLimits(10, 1024**2, 100, 100, 100, 16 * 1024**2, 60),
        "source_catalog_producer": producer(), "document_release_producer": producer(),
        "completed_at": "2026-09-11T00:00:00Z", "deadline_epoch_seconds": 4102444800,
    }
    initial = catalog(("kept", "missing"), "no-record-rejections")
    with prepare_local_experiment(initial.reference, workspace, **settings) as prepared:
        base = prepared.retain(prepared.run())
    partial = catalog(("kept",), "partial-rejection")
    with prepare_local_experiment(partial.reference, workspace, base_release=base, **settings) as prepared:
        retained = prepared.retain(prepared.run())
        view = open_local_inspection(prepared.plan, workspace, release_ref=retained,
            document_release_producer=producer(), source_catalog_producer=producer())
    assert [row["payload"]["disposition"] for row in view.records("dispositions")].count("deleted") == 1
    assert len(tuple(view.records("files"))) == 1
    assert view.summary()["work"]["counts"]["newCapturedFiles"] == 0
    assert view.summary()["source"]["sourceNativeInputs"][0]["collectionOutcome"]["recordOutcome"] == "partial-rejection"


@pytest.mark.parametrize("outcome", [{}, {"recordOutcome": "unresolved"}, {"recordOutcome": True}])
def test_unknown_or_invalid_local_outcome_labels_refuse(outcome):
    with pytest.raises(ValueError, match="unsupported recordOutcome"):
        replace(description(), collection_outcome=outcome)
