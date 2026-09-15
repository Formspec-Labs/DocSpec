"""Exercise literal GAO topics through the provider wheel and DocSpec catalog."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from rulespec_artifacts import LocalBlobSource
from spicy_docs.source_native.profiles import GAO_PRODUCT_PAGE_PROFILE
from spicy_docs.sources.gao.native import parse_gao_product_page_response

from docspec.domain.identity import sha256_digest
from docspec.domain.references import SourceCatalogRef
from docspec.runtime import open_local_catalog
from docspec.source_catalog import SpicyDocsSourceNativeAdapter
from examples import gao_topics as example

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the retained GAO topic example must not use a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _open_catalog(result, output):
    return open_local_catalog(SourceCatalogRef.from_dict(result["catalog"]), Path(output / "dataset"),
                              producer=example.catalog_producer())


@pytest.mark.parametrize(("case", "label", "slug", "matches"), [
    ("matching", "Information Security", "information-security", [example.PRODUCT_ID]),
    ("unexpected", "Agency Operations", "agency-operations", []),
])
def test_literal_topic_and_source_evidence_survive_catalog_mapping(tmp_path, case, label, slug, matches):
    output = tmp_path / case
    result = example.run_example(output, case=case)
    catalog = _open_catalog(result, output)
    row, = catalog.iter_mappings()
    metadata = row["sourceNativeFacts"][0]["fields"]["metadata"]
    topic = {"href": f"/topics/{slug}", "label": label, "slug": slug}
    native = metadata["record"]["record"]
    body = (example.FIXTURES / f"{case}.html").read_bytes()
    assert native["publisherTopic"] == topic
    assert native["htmlSha256"] == result["inputSha256"] == sha256_digest(body)
    assert metadata["source"]["artifactDigest"] == result["sourceResult"]["artifactDigest"]
    assert metadata["source"]["collectionOutcome"] == result["sourceResult"]["collectionOutcome"]
    assert row["selection"]["disposition"] == "unavailable" and row["candidateRenditions"] == []
    assert result["topicSelection"]["matchedDocumentIds"] == matches
    assert result["topicSelection"]["items"][0]["publisherTopic"] == topic
    assert result["topicSelection"]["items"][0]["source"] == {
        key: result["sourceResult"][key] for key in ("logicalId", "artifactDigest")
    }
    assert result["topicSelection"]["items"][0]["evidence"] == metadata["evidence"]

    source = SpicyDocsSourceNativeAdapter.from_local(Path(result["sourceRoot"]),
        blob_root=Path(result["sourceBlobRoot"]), profile=GAO_PRODUCT_PAGE_PROFILE,
        logical_id=metadata["source"]["logicalId"], artifact_digest=metadata["source"]["artifactDigest"],
        accepted_verifier_implementation_ids=frozenset({
            example.NAMESPACE + ":provider:" + result["provider"]["installedFilesSha256"],
        }))
    original, = source.iter_records()
    assert metadata["record"] == original
    assert metadata["evidence"] == source.record_evidence(example.PRODUCT_ID)
    evidence = source.read_evidence(metadata["evidence"]["evidenceBlobRef"], max_bytes=1024**2)
    assert parse_gao_product_page_response(evidence)["results"][0]["publisherTopic"] == topic
    with ZipFile(BytesIO(evidence)) as archive:
        assert archive.read("product.html") == body
    assert json.loads((output / "gao-topics.json").read_text()) == result


def test_exact_topic_filter_reuses_the_same_catalog_without_mutation(tmp_path):
    output = tmp_path / "experiment"
    result = example.run_example(output, case="unexpected")
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in output.rglob("*") if p.is_file()}
    catalog = _open_catalog(result, output)
    assert example.topic_selection(catalog, "Agency Operations")["matchedDocumentIds"] == [example.PRODUCT_ID]
    assert example.topic_selection(catalog, "agency operations")["matchedDocumentIds"] == []
    assert example.topic_selection(catalog, "Information Security")["matchedDocumentIds"] == []
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in output.rglob("*") if p.is_file()} == before


def test_missing_publisher_topic_preserves_source_refusal_without_a_catalog(tmp_path):
    output = tmp_path / "missing"
    result = example.run_example(output, case="missing")
    assert result["catalog"] is None and result["topicSelection"] is None
    assert not (output / "source").exists() and not (output / "dataset").exists()
    failure = result["sourceResult"]
    assert failure["ok"] is False and failure["error"]["code"] == "acquisition-failed"
    response = failure["failedAcquisition"]["response"]
    assert response["status"] == "retained"
    with LocalBlobSource(Path(result["sourceBlobRoot"])).open(response["blobRef"]) as stream:
        body = stream.read(response["byteSize"] + 1)
    assert body == (example.FIXTURES / "missing.html").read_bytes()
    assert sha256_digest(body) == result["inputSha256"]
    assert json.loads((output / "source-result.json").read_text()) == failure


def test_existing_example_output_is_not_replaced(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_bytes(b"existing result")
    with pytest.raises(ValueError, match="does not exist"):
        example.run_example(output)
    assert marker.read_bytes() == b"existing result" and list(output.iterdir()) == [marker]


def test_missing_case_does_not_hide_other_source_failures(tmp_path, monkeypatch):
    def fail(*args, stderr, **kwargs):
        stderr.write(json.dumps({"ok": False, "error": {"code": "destination-exists", "message": "unrelated failure"}}))
        return 1
    monkeypatch.setattr(example, "source_main", fail)
    output = tmp_path / "failure"
    with pytest.raises(RuntimeError, match="source publication failed"):
        example.run_example(output, case="missing")
    assert not (output / "gao-topics.json").exists() and not (output / "dataset").exists()
    assert json.loads((output / "source-result.json").read_text())["error"]["code"] == "destination-exists"


def test_example_command_filters_an_explicit_label(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
    result = subprocess.run([
        sys.executable, "-m", "examples.gao_topics", "--case", "unexpected", "--topic", "Agency Operations",
        "--output", str(tmp_path / "command"),
    ], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["topicSelection"]["matchedDocumentIds"] == [example.PRODUCT_ID]
    assert report["input"] == "one synthetic GAO page; no live requests"
