"""Annual publisher metadata, exact section XML, and reusable processing stay distinct."""

import json
import socket
from dataclasses import asdict, replace
from contextlib import closing

import httpx
import pytest
from spicy_docs.sources.cfr.acquisition import CfrAcquirer
from spicy_docs.sources.cfr.annual import annual_cfr_xml_locator, validate_annual_cfr_xml
from spicy_docs.sources.cfr.models import AnnualCfrSelection
from spicy_docs.sources.govinfo.mods import parse_govinfo_mods

from docspec.domain.content import CandidateFile
from docspec.domain.identity import sha256_digest
from docspec.application.document_processors import segment_rows
from examples.dataset_example_support import output_value
from docspec.errors import IntegrityError
from docspec.processing.artifacts import verify_representation_evidence, verify_segment_evidence
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from examples import govinfo_cfr as example
from examples.govinfo_cfr_fetcher import AnnualCfrContentFetcher
from tests.support.processing import _captured


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the CFR qualification attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _responses(monkeypatch, *, metadata=None, body=None, status=200, content_type="application/xml"):
    metadata = (example.FIXTURES / "edition.xml").read_bytes() if metadata is None else metadata
    body = (example.FIXTURES / "section.xml").read_bytes() if body is None else body

    def transport(requests):
        def respond(request):
            requests.append(str(request.url))
            is_mods = request.url.path.endswith("mods.xml")
            return httpx.Response(200 if is_mods else status,
                stream=httpx.ByteStream(metadata if is_mods else body),
                headers={"Content-Type": "application/xml" if is_mods else content_type})
        return httpx.MockTransport(respond)
    monkeypatch.setattr(example, "fixture_transport", transport)


def test_complete_metadata_and_source_spans_survive_processing_with_closed_client(tmp_path, monkeypatch):
    completed, acquired, closed = [], [], []
    original = (example.FIXTURES / "section.xml").read_bytes()
    metadata_bytes = (example.FIXTURES / "edition.xml").read_bytes()
    finish, acquire, close = example.run_documents, CfrAcquirer.acquire_annual, CfrAcquirer.close

    def acquire_section(client, selection, **kwargs):
        assert client not in closed
        acquired.append(selection)
        return acquire(client, selection, **kwargs)

    def close_client(client):
        closed.append(client)
        return close(client)

    def inspect_finish(workspace, pipeline, source_state_id, **kwargs):
        if completed:
            assert closed
        result, documents = finish(workspace, pipeline, source_state_id, **kwargs)
        stages = documents[0][1]
        captured = output_value(workspace, stages[0], "capture")
        assert captured["transportVersion"] is None
        assert output_value(workspace, stages[0], "content") == original
        with workspace.publisher.session() as session:
            segments_id = stages[2].outcome.outputs[0].entity_id
            with closing(segment_rows(session, segments_id)) as rows:
                segments = {key: (segment, reference) for key, segment, reference in rows}
            output_id = stages[3].outcome.outputs[0].entity_id
            for key, _, value in pipeline.rows(output_id):
                if ":output:" not in key:
                    continue
                segment, reference = segments[key.split(":output:")[0]]
                with closing(session.blobs.read(reference, max_bytes=4096)) as chunks:
                    content = b"".join(chunks)
                evidence = value["enclosingSourceEvidence"]
                assert evidence == segment.evidence.to_dict() and evidence["sourceDigest"] == sha256_digest(original)
                assert 0 <= evidence["start"] < evidence["end"] <= len(original)
                for match in value["matches"]:
                    assert content[match["segmentByteStart"]:match["segmentByteEnd"]].decode() == match["quote"]
        completed.append(result)
        return result, documents

    monkeypatch.setattr(CfrAcquirer, "acquire_annual", acquire_section)
    monkeypatch.setattr(CfrAcquirer, "close", close_client)
    monkeypatch.setattr(example, "run_documents", inspect_finish)
    output = tmp_path / "experiment"
    summary = example.run_example(output)
    assert len(completed) == 2 and acquired == [example.FIXTURE_SELECTION]
    assert summary["reprocessingNewWork"] == {"newCapturedFiles": 0, "newRepresentations": 0, "newSegments": 0}
    assert summary["originalLayersPreserved"]
    assert len(summary["matches"]["first"]) == 2 and len(summary["matches"]["later"]) == 3
    assert summary["capturedSha256"] == sha256_digest(original)
    assert "## Public access" in summary["visibleText"] and "records & résumé metadata." in summary["visibleText"]
    assert "Machine readable" in summary["visibleText"] and "<E" not in summary["visibleText"]
    assert "2023-01-01" in summary["visibleText"]
    assert summary["edition"]["date_issued"] == "2025-01-01"
    assert summary["edition"]["original_date_issued"] == "2023-01-01" and summary["edition"]["is_cover_only"] is True
    assert (output / "source-evidence/cfr-mods.xml").read_bytes() == metadata_bytes
    preview = json.loads((output / "catalog-preview.json").read_text())
    item, = preview["items"]
    fields = item["sourceNativeFacts"][0]["fields"]["metadata"]
    parsed = parse_govinfo_mods(metadata_bytes)
    assert fields["mods"] == json.loads(json.dumps(asdict(parsed)))
    selected = parsed.constituents[1]
    assert fields["selectedConstituentPath"] == list(selected.element.path)
    assert fields["selectedUrlPath"] == list(selected.urls[1].path)
    assert item["sourceIssuedVersion"] == "CFR-2025-title1-vol1-sec18-1"
    assert len(item["candidateRenditions"]) == 1
    assert item["candidateRenditions"][0]["locator"] == annual_cfr_xml_locator(example.FIXTURE_SELECTION)
    assert summary["metadataConstituentCount"] == 2 and summary["metadataUrlCount"] == 3
    text_receipt = json.loads(next((output / "source-evidence").glob("cfr-text-*.json")).read_text())
    assert text_receipt["identity"]["source"] == "annual-cfr"
    assert text_receipt["identity"]["stated_date"] == "2023-01-01"
    assert text_receipt["modsSha256"] == sha256_digest(metadata_bytes)
    assert summary["fixtureRequests"] == [example.annual_cfr_edition_locator(replace(example.FIXTURE_SELECTION, section=None)),
                                           annual_cfr_xml_locator(example.FIXTURE_SELECTION)]


@pytest.mark.parametrize("problem", ["missing-section", "duplicate-record", "duplicate-url", "foreign-edition", "ecfr-only"])
def test_selection_refuses_unstated_or_ambiguous_annual_xml(tmp_path, monkeypatch, problem):
    metadata = (example.FIXTURES / "edition.xml").read_text()
    selected = metadata[metadata.index('  <relatedItem type="constituent" ID="id-CFR-2025-title1-vol1-sec18-1"'):]
    selected = selected[:selected.index("</relatedItem>") + len("</relatedItem>")]
    url = annual_cfr_xml_locator(example.FIXTURE_SELECTION)
    node = f'<url displayLabel="XML rendition" access="raw object">{url}</url>'
    if problem == "missing-section":
        metadata = metadata.replace(selected, "")
    elif problem == "duplicate-record":
        metadata = metadata.replace(selected, selected + selected)
    elif problem == "duplicate-url":
        metadata = metadata.replace(node, node + node)
    else:
        replacement = url.replace("2025", "2023") if problem == "foreign-edition" else "https://www.ecfr.gov/api/versioner/v1/full/2025-01-01/title-1.xml"
        metadata = metadata.replace(url, replacement)
    _responses(monkeypatch, metadata=metadata.encode())
    output = tmp_path / problem
    with pytest.raises(ValueError, match="annual section requires"):
        example.run_example(output)
    assert (output / "source-evidence/cfr-mods.xml").read_bytes() == metadata.encode()
    assert (output / "source-evidence/cfr-selection-refusal.json").exists()
    assert not list((output / "source-evidence").glob("cfr-text*"))
    assert not (output / "cfr-example-summary.json").exists()


@pytest.mark.parametrize("problem", ["wrong-section", "unavailable", "html"])
def test_failed_capture_keeps_refused_body_and_does_not_publish_success(tmp_path, monkeypatch, problem):
    body = (example.FIXTURES / "section.xml").read_bytes()
    status, content_type = 200, "application/xml"
    if problem == "wrong-section":
        body = body.replace(b"18.1", b"18.2")
    elif problem == "unavailable":
        body, status, content_type = b"Gone", 404, "text/plain"
    else:
        body, content_type = b"<html><body>Document unavailable</body></html>", "text/html"
    _responses(monkeypatch, body=body, status=status, content_type=content_type)
    output = tmp_path / problem
    with pytest.raises((RuntimeError, ValueError)):
        example.run_example(output)
    refusal = json.loads(next((output / "source-evidence").glob("cfr-text-*-refusal.json")).read_text())
    assert refusal["acquisition"]["operation"] == "annual-cfr" and refusal["acquisition"]["requestCount"] == 1
    assert refusal["response"]["sha256"] == sha256_digest(body)
    assert (output / "source-evidence" / refusal["response"]["bodyFile"]).read_bytes() == body
    assert (output / "processed-failures.json").exists() and not (output / "cfr-example-summary.json").exists()


def test_candidate_change_refuses_before_body_request_and_byte_limit_is_applied(tmp_path):
    requests = []
    with CfrAcquirer(budget=example.CfrAcquisitionBudget(1, 4096, 20, 0),
                     transport=example.fixture_transport(requests)) as acquirer:
        edition = acquirer.acquire_annual_edition(replace(example.FIXTURE_SELECTION, section=None))
        fetcher = AnnualCfrContentFetcher(acquirer, edition, example.FIXTURE_SELECTION, tmp_path,
                                         lambda: example.FIXTURE_TIME)
        for candidate in (CandidateFile("x", "https://example.test/other.xml", "application/xml"),
                          CandidateFile("x", fetcher.locator, "text/html")):
            with pytest.raises(IntegrityError, match="explicitly selected annual section"):
                fetcher.fetch(candidate, max_bytes=1024, task_id="task", attempt_id="attempt")
        assert len(requests) == 1
        with pytest.raises(ValueError, match="exceeds"):
            fetcher.fetch(CandidateFile("x", fetcher.locator, "application/xml"),
                          max_bytes=32, task_id="task", attempt_id="attempt")
    refusal = json.loads(next(tmp_path.glob("cfr-text-*-refusal.json")).read_text())
    assert refusal["acquisition"]["budget"]["max_bytes"] == 32


def test_real_annual_granule_has_visible_blocks_with_replayable_source_spans():
    source = (example.FIXTURES / "real-section716-2.xml").read_bytes()
    selection = AnnualCfrSelection(2025, 30, 3, "716.2")
    native = validate_annual_cfr_xml(source, identity=selection, final_url=annual_cfr_xml_locator(selection))
    assert native.section == "716.2" and native.root_tag == "CFRGRANULE"
    captured = _captured(source, "application/xml")
    extractor = VisibleTextExtractor(xml_heading_levels=example.CFR_HEADINGS)
    result = extractor.extract(captured, source)
    segments = VisibleTextBlockSegmenter().segment(result.payload)
    assert b"## Steep-slope mining." in result.payload.content
    assert b"Variances from approximate original contour restoration requirements." in result.payload.content
    assert b"<PRTPAGE" not in result.payload.content and b"<E " not in result.payload.content
    resolver = extractor.evidence_resolver(captured, source)
    verify_representation_evidence(result.payload, source, derived_resolver=resolver)
    for segment in segments:
        verify_segment_evidence(segment, result.payload, source, derived_resolver=resolver)


def test_existing_output_and_volume_only_selection_refuse(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep"
    marker.write_bytes(b"prior evidence")
    with pytest.raises(ValueError, match="does not exist"):
        example.run_example(existing)
    assert marker.read_bytes() == b"prior evidence"
    with pytest.raises(ValueError, match="explicit annual section"):
        example.run_example(tmp_path / "volume", selection=replace(example.FIXTURE_SELECTION, section=None))
