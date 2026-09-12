"""Public comment-table facts remain useful before any document acquisition."""

from __future__ import annotations

import json
import socket
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from spicy_docs.source_native_profiles import SPICY_REGS_PUBLIC_COMMENT_PROFILE
from spicy_docs.sources.public_comments.native import PARTITION_ENTRY, comment_partition_locator

from docspec.domain.identity import identity_digest, sha256_digest
from docspec.domain.references import SourceCatalogRef
from docspec.errors import LimitExceededError
from docspec.runtime import open_local_catalog
from docspec.source_catalog import SpicyDocsSourceNativeAdapter
from docspec.workspace import LocalWorkspace
from examples import spicyregs_comments as example


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the comment example must never fetch a table or attachment over the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _catalog(report, output):
    return open_local_catalog(SourceCatalogRef.from_dict(report["catalog"]), LocalWorkspace(output / "dataset"),
                              producer=example.catalog_producer())


def test_table_facts_nulls_diagnostics_and_candidates_survive_installed_mapping(tmp_path):
    output = tmp_path / "comments"
    report = example.run_example(output)
    catalog = _catalog(report, output)
    rows = {row["documentId"]: row for row in catalog.iter_mappings()}
    assert set(rows) == {f"EPA-2026-0001-{number:04d}" for number in range(1, 4)}
    source = SpicyDocsSourceNativeAdapter.from_local(Path(report["sourceRoot"]),
        blob_root=Path(report["sourceBlobRoot"]), profile=SPICY_REGS_PUBLIC_COMMENT_PROFILE,
        logical_id=report["sourceResult"]["logicalId"], artifact_digest=report["sourceResult"]["artifactDigest"],
        accepted_verifier_implementation_ids=frozenset({
            example.NAMESPACE + ":provider:" + report["provider"]["installedFilesSha256"],
        }))
    originals = {row["sourceRecordId"]: row for row in source.iter_records()}
    for identifier, row in rows.items():
        metadata = row["sourceNativeFacts"][0]["fields"]["metadata"]
        assert metadata["record"] == originals[identifier]
        assert metadata["source"] == source.describe().to_dict()
        assert metadata["evidence"] == source.record_evidence(identifier)
        assert row["sourceIssuedVersion"] == identity_digest(originals[identifier])
        pack = source.read_evidence(metadata["evidence"]["evidenceBlobRef"], max_bytes=example.MAX_BYTES)
        with ZipFile(BytesIO(pack)) as archive:
            assert archive.read(PARTITION_ENTRY) == (output / "input.parquet").read_bytes()

    first = rows["EPA-2026-0001-0001"]
    first_metadata = first["sourceNativeFacts"][0]["fields"]["metadata"]
    native = first_metadata["record"]["record"]
    assert native["title"] == "Café access" and native["comment"] == "See attached"
    assert native["agency_code"] == "EPA" and native["organization"] is None
    assert native["text_content"] is None and native["text_extraction_status"] is None
    assert len(native) == 16
    candidate, = first["candidateRenditions"]
    assert candidate == {
        "renditionId": "attachment-0000-0000", "mediaType": "application/pdf", "locatorKind": "source-url",
        "locator": example.ATTACHMENT, "expectedSha256": None, "expectedByteSize": 123,
    }
    assert first_metadata["renditions"][0]["sourceField"] == "attachments_json[0].formats[0]"
    assert first["selection"]["disposition"] == "selected"
    second = rows["EPA-2026-0001-0002"]["sourceNativeFacts"][0]["fields"]["metadata"]["record"]["record"]
    assert second["comment"] == "Please preserve café access."
    assert second["text_content"] == "Table-supplied extracted text" and second["modify_date"] is None
    third = rows["EPA-2026-0001-0003"]["sourceNativeFacts"][0]["fields"]["metadata"]["record"]
    assert third["record"]["attachments_json"] == "[broken JSON" and third["record"]["comment"] == ""
    assert third["fieldDiagnostics"] and third["fieldDiagnostics"][0]["field"] == "attachments_json"
    for identifier in ("EPA-2026-0001-0002", "EPA-2026-0001-0003"):
        assert rows[identifier]["candidateRenditions"] == []
        assert rows[identifier]["selection"]["disposition"] == "unavailable"
    assert report["sourceRecordRejections"] == []
    outcome = report["sourceResult"]["collectionOutcome"]
    assert outcome["sourceStateScope"] == "observed-crawl"
    assert outcome["traversalAcceptance"] == "single-observed-traversal"
    assert outcome["recordOutcome"] == "no-record-rejections"
    assert report["preview"]["catalog"]["catalogSelection"]["counts"] == {
        "selected": 1, "unavailable": 2, "excluded": 0, "deleted": 0, "failed": 0,
    }
    assert report["preview"]["catalog"]["sourceNativeInputs"][0]["collectionOutcome"] is None
    assert report["fixtureTableProbes"] == [comment_partition_locator("EPA", index) for index in (0, 1)]
    assert report["partitionSha256"] == sha256_digest((output / "input.parquet").read_bytes())
    assert report["documentProcessing"] == {"status": "not requested", "capturedFiles": 0, "capturedBytes": 0}
    assert json.loads((output / "spicyregs-comments.json").read_text()) == report
    workspace = LocalWorkspace(output / "dataset")
    assert all(not root.exists() for role, root in workspace.roots.items() if role != "sourceCatalog")


def test_metadata_filter_reuses_catalog_and_does_not_treat_null_or_empty_as_text(tmp_path):
    output = tmp_path / "comments"
    report = example.run_example(output)
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in output.rglob("*") if p.is_file()}
    catalog = _catalog(report, output)
    selected = example.inspect_comments(catalog, "EPA-2026-0001")
    assert set(selected["matchedDocumentIds"]) == {"EPA-2026-0001-0001", "EPA-2026-0001-0003"}
    assert example.inspect_comments(catalog, "EPA-2026-0002")["matchedDocumentIds"] == ["EPA-2026-0001-0002"]
    assert example.inspect_comments(catalog, "epa-2026-0001")["matchedDocumentIds"] == []
    missing = {row["documentId"]: row["unavailableFields"] for row in selected["items"]}
    assert "modify_date" in missing["EPA-2026-0001-0002"]
    assert "comment" not in missing["EPA-2026-0001-0003"]  # Empty text remains an observed value.
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in output.rglob("*") if p.is_file()} == before


def test_invalid_row_refuses_the_partition_without_fabricating_a_partial_catalog(tmp_path):
    output = tmp_path / "invalid"
    report = example.run_example(output, invalid_row=True)
    assert report["sourceResult"]["error"]["code"] == "acquisition-failed"
    assert "row lacks comment_id" in report["sourceResult"]["error"]["message"]
    assert report["catalog"] is None and report["preview"] is None and report["commentInspection"] is None
    assert report["sourceRecordRejections"] is None  # Whole-input refusal did not publish a row-rejection ledger.
    assert not (output / "source").exists() and not (output / "dataset").exists()
    assert sha256_digest((output / "input.parquet").read_bytes()) == report["partitionSha256"]


def test_example_bound_refuses_before_catalog_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(example, "MAX_RECORDS", 2)
    output = tmp_path / "bounded"
    with pytest.raises(LimitExceededError, match="record limit"):
        example.run_example(output)
    assert (output / "source").exists() and not (output / "dataset").exists()


def test_existing_output_is_never_replaced_and_other_source_errors_are_not_hidden(tmp_path, monkeypatch):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep"
    marker.write_bytes(b"existing result")
    with pytest.raises(ValueError, match="does not exist"):
        example.run_example(output)
    assert list(output.iterdir()) == [marker] and marker.read_bytes() == b"existing result"

    def fail(*args, stderr, **kwargs):
        stderr.write(json.dumps({"error": {"code": "destination-exists", "message": "unrelated failure"}}))
        return 1
    monkeypatch.setattr(example, "source_main", fail)
    with pytest.raises(RuntimeError, match="source publication failed"):
        example.run_example(tmp_path / "unrelated", invalid_row=True)
    assert not (tmp_path / "unrelated/dataset").exists()
