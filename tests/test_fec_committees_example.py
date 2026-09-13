"""A bounded FEC census keeps source facts without claiming document acquisition."""

import copy
import json
import socket
import sys
from contextlib import contextmanager
from io import BytesIO
from zipfile import ZipFile

import pytest
from rulespec_artifacts import ArtifactPin, LocalBlobSource, LocalMemberSource
from spicy_docs.source_native import SourceNativeReleaseReader

from docspec.domain.references import SourceCatalogRef
from docspec.errors import LimitExceededError
from docspec.runtime import open_local_catalog
from docspec.workspace import LocalWorkspace
from examples import fec_committees as example


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the retained FEC example attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _reader(inputs, *, blob_source=None, **overrides):
    arguments = {
        "blob_source": blob_source or LocalBlobSource(inputs["blob_root"]), "profile": example.PROFILE,
        "expected_pin": ArtifactPin(inputs["logical_id"], inputs["artifact_digest"]),
        "accepted_verifier_implementation_ids": frozenset({inputs["source_implementation_id"]}),
    }
    return SourceNativeReleaseReader(LocalMemberSource(inputs["source_root"]), **(arguments | overrides))


def _catalog(output, summary):
    return open_local_catalog(SourceCatalogRef.from_dict(summary["catalog"]),
                              LocalWorkspace(output / "dataset"), producer=example.catalog_producer())


def _build(reader, output, **overrides):
    limits = {"max_records": 2, "max_bytes": 1024**2, "max_scratch_bytes": 16 * 1024**2}
    return example.build_committee_catalog(reader, LocalWorkspace(output), **(limits | overrides))


def test_complete_source_facts_and_evidence_survive_one_ordered_join(tmp_path, monkeypatch):
    inputs = example.publish_fixture(tmp_path / "retained")
    reader = _reader(inputs)
    expected = list(reader.iter_records())
    evidence = list(reader.iter_record_evidence())
    calls = []
    for name in ("iter_records", "iter_record_evidence", "iter_renditions"):
        original = getattr(reader, name)

        def counted(original=original, name=name):
            calls.append(name)
            yield from original()

        monkeypatch.setattr(reader, name, counted)

    def no_lookup(_):
        raise AssertionError("a bulk catalog must not rescan evidence for each record")

    monkeypatch.setattr(reader, "record_evidence", no_lookup)
    workspace = LocalWorkspace(tmp_path / "dataset")
    result = _build(reader, workspace.root)
    assert calls == ["iter_renditions", "iter_records", "iter_record_evidence"]
    catalog = open_local_catalog(result.reference, workspace, producer=example.catalog_producer())
    actual = {row["documentId"]: row for row in catalog.iter_mappings()}
    assert len(actual) == catalog.summary.item_count == 2
    for original, observation in zip(expected, evidence, strict=True):
        item = actual[reader.source_system_id + ":" + original["sourceRecordId"]]
        supplied = item["sourceNativeFacts"][0]["fields"]
        assert supplied["metadata"]["record"] == original
        assert supplied["metadata"]["evidence"] == observation
        assert item["sourceIssuedVersion"] == observation["evidenceBlobRef"]
        assert supplied["metadata"]["versionMeaning"] == example.VERSION_MEANING
        assert item["normalizedMetadata"]["title"] == original["record"]["metadata"].get("name")
        assert item["candidateRenditions"] == []
        assert item["selection"]["disposition"] == "unavailable"
        assert "requestedScope" not in supplied["metadata"]["source"]["collectionOutcome"]
        assert supplied["metadata"]["source"]["artifactDigest"] == inputs["artifact_digest"]
    assert expected[0]["record"]["metadata"]["future_metadata"]["unknown"] == {"empty": [], "null": None}
    assert expected[0]["record"]["assets"][0]["url"] == "https://www.fec.gov/synthetic-example.pdf"
    with ZipFile(BytesIO(reader.read_evidence(evidence[0]["evidenceBlobRef"]))) as archive:
        assert archive.read("response.json") == (example.FIXTURES / "committees.json").read_bytes()
    assert {path.name for path in workspace.root.iterdir()} == {"sourceCatalog"}


def test_offline_and_existing_release_paths_keep_full_scope_once(tmp_path):
    output = tmp_path / "offline"
    summary = example.run_example(output)
    assert summary["synthetic"] and summary["itemCount"] == 2
    assert summary["dispositions"]["unavailable"] == 2
    outcome = summary["source"]["collectionOutcome"]
    assert outcome["recordOutcome"] == "no-record-rejections" and outcome["publishedRecordCount"] == 2
    assert outcome["sourceStateScope"] == "observed-crawl"
    assert "cycle=2026" in outcome["requestedScope"]["captures"][0]["requestUrl"]
    assert json.loads((output / "fec-example-summary.json").read_text()) == summary
    source_input = summary["input"]
    repeated_output = tmp_path / "existing"
    repeated = example.run_example(repeated_output,
        source_root=output / "source", blob_root=output / "source-blobs",
        logical_id=source_input["logical_id"], artifact_digest=source_input["artifact_digest"],
        source_implementation_id=source_input["source_implementation_id"])
    assert not repeated["synthetic"] and repeated["source"] == summary["source"]
    assert list(_catalog(repeated_output, repeated).iter_mappings()) == list(_catalog(output, summary).iter_mappings())
    assert not (repeated_output / "source").exists()
    assert not (repeated_output / "source-blobs").exists()


def test_empty_observed_census_produces_empty_catalog_without_global_absence_claim(tmp_path):
    output = tmp_path / "empty"
    summary = example.run_example(output, empty=True, max_records=1, max_bytes=1)
    assert summary["itemCount"] == 0 and all(value == 0 for value in summary["dispositions"].values())
    assert summary["source"]["sourceStateScope"] == "observed-crawl"
    assert summary["source"]["collectionOutcome"]["recordOutcome"] == "empty"
    assert list(_catalog(output, summary).iter_mappings()) == []


@pytest.mark.parametrize("problem", ["missing-evidence", "wrong-evidence", "failed-evidence", "rendition", "identity"])
def test_mapping_refuses_to_discard_source_membership_or_supplied_bodies(tmp_path, monkeypatch, problem):
    reader = _reader(example.publish_fixture(tmp_path / "retained"))
    rows = list(reader.iter_records())
    evidence = list(reader.iter_record_evidence())
    if problem == "missing-evidence":
        evidence.pop()
    elif problem == "wrong-evidence":
        evidence[1]["sourceRecordId"] = "C99999999"
    elif problem == "failed-evidence":
        evidence[1]["failure"] = {"reason": "rejected"}
    elif problem == "rendition":
        monkeypatch.setattr(reader, "iter_renditions", lambda: (row for row in [{"sourceRecordId": "C00000001"}]))
    else:
        rows[0]["record"]["metadata"]["committee_id"] = "C99999999"
    monkeypatch.setattr(reader, "iter_records", lambda: (row for row in rows))
    monkeypatch.setattr(reader, "iter_record_evidence", lambda: (row for row in evidence))
    with pytest.raises(ValueError):
        _build(reader, tmp_path / "catalog")
    assert not (tmp_path / "catalog").exists()


@pytest.mark.parametrize("outcome", ["partial-rejection", "total-rejection", "count-mismatch"])
def test_source_rejections_and_changed_population_do_not_become_success(tmp_path, monkeypatch, outcome):
    reader = _reader(example.publish_fixture(tmp_path / "retained"))
    describe = example.source_description
    changed = copy.deepcopy(describe(reader))
    if outcome == "count-mismatch":
        changed["collectionOutcome"]["publishedRecordCount"] += 1
    else:
        changed["collectionOutcome"]["recordOutcome"] = outcome
    monkeypatch.setattr(example, "source_description", lambda _: changed)
    with pytest.raises(ValueError, match="rejected records|count differs"):
        _build(reader, tmp_path / "catalog")
    assert not (tmp_path / "catalog").exists()


@pytest.mark.parametrize("problem", ["missing", "malformed"])
def test_public_reader_refuses_missing_or_changed_retained_evidence(tmp_path, problem):
    inputs = example.publish_fixture(tmp_path / "retained")
    reader = _reader(inputs)
    evidence_ref = next(reader.iter_record_evidence())["evidenceBlobRef"]
    original = LocalBlobSource(inputs["blob_root"])

    class DamagedEvidence:
        @contextmanager
        def open(self, digest):
            if digest == evidence_ref:
                if problem == "missing":
                    raise FileNotFoundError("selected source evidence is missing")
                yield BytesIO(b"not retained evidence")
            else:
                with original.open(digest) as stream:
                    yield stream

    with pytest.raises((ValueError, FileNotFoundError)):
        _reader(inputs, blob_source=DamagedEvidence())


def test_existing_release_requires_exact_pin_and_explicit_verifier_acceptance(tmp_path):
    inputs = example.publish_fixture(tmp_path / "retained")
    with pytest.raises(ValueError):
        _reader(inputs, expected_pin=ArtifactPin(inputs["logical_id"], "sha256:" + "0" * 64))
    with pytest.raises(ValueError, match="not accepted"):
        _reader(inputs, accepted_verifier_implementation_ids=frozenset({"another-verifier"}))


@pytest.mark.parametrize("bound", [{"max_records": 1}, {"max_bytes": 1}, {"max_scratch_bytes": 1}])
def test_catalog_bounds_are_enforced_by_the_existing_components(tmp_path, bound):
    reader = _reader(example.publish_fixture(tmp_path / "retained"))
    with pytest.raises((ValueError, LimitExceededError)):
        _build(reader, tmp_path / "catalog", **bound)


def test_existing_output_and_partial_source_options_are_refused_before_writing(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(ValueError, match="does not exist"):
        example.run_example(output)
    assert marker.read_text() == "keep" and list(output.iterdir()) == [marker]
    absent = tmp_path / "absent"
    with pytest.raises(ValueError, match="requires both roots"):
        example.run_example(absent, source_root=tmp_path / "retained")
    assert not absent.exists()


def test_cli_reports_empty_census_and_refuses_reusing_its_output(tmp_path, monkeypatch, capsys):
    output = tmp_path / "cli-empty"
    monkeypatch.setattr(sys, "argv", ["fec_committees", "--empty", "--output", str(output)])
    assert example.main() == 0
    assert json.loads(capsys.readouterr().out)["itemCount"] == 0
    with pytest.raises(SystemExit) as error:
        example.main()
    assert error.value.code == 2
    assert "does not exist" in capsys.readouterr().err
