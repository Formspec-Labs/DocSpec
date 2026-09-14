"""Copied beside the examples and executed by the installed-wheel qualification."""

import hashlib
import json
import socket
from examples.storage_network import storage_only
import sys
import zipfile
from dataclasses import asdict, replace
from contextlib import closing
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import docspec
import httpx
import spicy_docs
from spicy_docs.sources.congress.bill_acquisition import BillAcquirer

from docspec.domain.identity import identity_digest, sha256_digest
from docspec.domain.content import CandidateFile
from docspec.application.document_processors import segment_rows
from docspec.runtime import CoreWorkspace
from examples import govinfo_bills as example
from examples.provider_identity import provider_installation
from examples.govinfo_bill_fetcher import BillContentFetcher
from examples.dataset_example_support import retain_refusal, output_value


def main():
    root, proof, wheel = map(Path, sys.argv[1:4])
    wheel_sha = sys.argv[4]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == wheel_sha
    for module in (docspec, spicy_docs):
        assert "site-packages" in Path(module.__file__).parts
    with zipfile.ZipFile(wheel) as archive:
        installed = Path(spicy_docs.__file__).parent.parent
        for name in archive.namelist():
            if name.startswith("spicy_docs/") and not name.endswith("/"):
                assert (installed / name).read_bytes() == archive.read(name), name
    original = (example.FIXTURES / "introduced.xml").read_bytes()
    status = (example.FIXTURES / "status.xml").read_bytes()
    completed, text_calls, closed = [], [], []
    finish, acquire_text, close = example.run_documents, BillAcquirer.acquire_text, BillAcquirer.close

    def observed_text(client, *args, **kwargs):
        assert client not in closed, "reprocessing called the closed source client"
        text_calls.append(kwargs["package_id"])
        return acquire_text(client, *args, **kwargs)

    def observed_close(client):
        closed.append(client)
        return close(client)

    def inspected_finish(workspace, pipeline, source_state_id, **kwargs):
        if completed:
            assert closed, "the example must close acquisition before separate processing"
        result, documents = finish(workspace, pipeline, source_state_id, **kwargs)
        stages = documents[0][1]
        captured = output_value(workspace, stages[0], "capture")
        assert captured["transportVersion"] is None
        assert captured["sourceVersion"] == "BILLS-119hr6028ih"
        assert output_value(workspace, stages[0], "content") == original
        with workspace.publisher.session() as session:
            with closing(segment_rows(session, stages[2].outcome.outputs[0].entity_id)) as rows:
                segments = {key: (segment, reference) for key, segment, reference in rows}
            for key, _, value in pipeline.rows(stages[3].outcome.outputs[0].entity_id):
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
                    assert match["quote"] in original[evidence["start"]:evidence["end"]].decode()
        completed.append(result)
        return result, documents

    with patch.object(socket.socket, "connect", storage_only(socket.socket.connect)), patch.object(socket, "create_connection", storage_only(socket.create_connection)):
        output = root / "experiment"
        with patch.object(BillAcquirer, "acquire_text", observed_text), patch.object(BillAcquirer, "close", observed_close), \
                patch.object(example, "run_documents", inspected_finish):
            summary = example.run_example(output, package_id="BILLS-119hr6028ih")
        assert len(completed) == 2 and text_calls == ["BILLS-119hr6028ih"]
        assert summary["offeredTextVersions"] == 2 and summary["offeredFormats"] == 5
        assert summary["originalLayersPreserved"] and summary["reprocessingNewCaptureCount"] == 0
        assert summary["fixtureRequests"] == [example.bill_status_locator(example.FIXTURE_IDENTITY),
                                             example.bill_xml_locator(example.FIXTURE_IDENTITY, "BILLS-119hr6028ih")]
        assert summary["capturedSha256"] == sha256_digest(original)
        assert "records & résumé metadata." in summary["visibleText"]
        assert "<emphasis>" not in summary["visibleText"] and "## Public access" in summary["visibleText"]
        # The generic XML extractor also includes metadata; these are document-wide lexical matches.
        assert "119 HR 6028 IH:" in summary["visibleText"]
        receipt = json.loads((output / "source-evidence/bill-status-acquisition.json").read_text())
        assert (output / "source-evidence/bill-status.xml").read_bytes() == status
        assert receipt["capture"]["sha256"] == sha256_digest(status)
        preview = json.loads((output / "catalog-preview.json").read_text())
        assert preview["items"][0]["sourceIssuedVersion"] == "BILLS-119hr6028ih"
        source_facts = json.dumps(preview)
        for label in ("BILLS-119hr6028eh", "BILLS-119hr6028ih", "Formatted Text", "PDF"):
            assert label in source_facts
        assert "pdf/BILLS-119hr6028eh.pdf" in source_facts and "html/BILLS-119hr6028eh.htm" in source_facts
        saved = (output / "bill-example-summary.json").read_bytes()
        try:
            example.run_example(output, package_id="BILLS-119hr6028ih")
        except ValueError:
            pass
        else:
            raise AssertionError("existing output must refuse")
        assert (output / "bill-example-summary.json").read_bytes() == saved
        unavailable = root / "unavailable-selection"
        try:
            example.run_example(unavailable, package_id="BILLS-119hr6028rh")
        except ValueError:
            pass
        else:
            raise AssertionError("unoffered package must refuse")
        assert (unavailable / "source-evidence/bill-status.xml").read_bytes() == status
        assert (unavailable / "source-evidence/bill-selection-refusal.json").exists()
        assert not list((unavailable / "source-evidence").glob("bill-text*"))
        for name, response_status, media_type, body in (
            ("unavailable", 404, "text/plain", b"Gone"),
            ("wrong-bill", 200, "application/xml", original.replace(b"6028", b"6029")),
            ("placeholder", 200, "text/html", b"<html><body>Document unavailable</body></html>"),
        ):
            refused = root / name

            def refused_transport(requests):
                def respond(request):
                    requests.append(str(request.url))
                    is_status = str(request.url) == example.bill_status_locator(example.FIXTURE_IDENTITY)
                    return httpx.Response(200 if is_status else response_status,
                                          stream=httpx.ByteStream(status if is_status else body),
                                          headers={"Content-Type": "application/xml" if is_status else media_type})
                return httpx.MockTransport(respond)

            with patch.object(example, "fixture_transport", refused_transport):
                try:
                    example.run_example(refused, package_id="BILLS-119hr6028ih")
                except Exception as error:
                    assert str(error)
                else:
                    raise AssertionError(f"{name} must not produce a successful experiment summary")
            refusal = json.loads(next((refused / "source-evidence").glob("bill-text-*-refusal.json")).read_text())
            assert refusal["acquisition"]["requestCount"] == 1
            assert refusal["message"] and refusal["errorType"]
            assert refusal["response"]["sha256"] == sha256_digest(body)
            assert (refused / "source-evidence" / refusal["response"]["bodyFile"]).read_bytes() == body
            with CoreWorkspace(refused) as workspace:
                with workspace.ledger._transaction() as connection:
                    assert connection.execute("SELECT count(*) FROM records WHERE kind='result' AND json_extract(payload, '$.outcome.status')='failed'").fetchone() == (1,)
            assert (refused / "processed-failures.json").exists()
            assert not (refused / "bill-example-summary.json").exists()
        saved_error = ValueError("source refusal")
        retain_refusal(saved_error, root / "previously-absent/nested/refusal.json")
        assert json.loads((root / "previously-absent/nested/refusal.json").read_text())["message"] == str(saved_error)
        budget = example.BillAcquisitionBudget(2, 4096, 4096, 20, 0)
        with BillAcquirer(budget=budget, transport=example.fixture_transport([])) as reader:
            retained_status = reader.acquire_status(example.FIXTURE_IDENTITY)
        changed = replace(budget, max_requests=1, max_text_bytes=2048)
        with BillAcquirer(budget=changed, transport=example.fixture_transport([]),
                          clock=lambda: example.FIXTURE_TIME) as acquirer:
            fetcher = BillContentFetcher(acquirer, retained_status, "BILLS-119hr6028ih", root / "budget-receipts",
                                         clock=lambda: example.FIXTURE_TIME - timedelta(seconds=1))
            assert fetcher.configuration_digest == identity_digest({
                "provider": provider_installation(), "billStatusSha256": retained_status.capture.sha256,
                "packageId": "BILLS-119hr6028ih", "format": "application/xml", "budget": asdict(changed),
            })
            stream = fetcher.fetch(CandidateFile("example", fetcher.locator, "application/xml"),
                                   max_bytes=2048, task_id="task", attempt_id="attempt")
            assert b"".join(stream.chunks) == original
            assert stream.metadata.transport_version is None
            assert stream.metadata.acquisition_started_at == "2026-09-12T11:59:59Z"
            receipt = json.loads(next((root / "budget-receipts").glob("*.json")).read_text())
            assert receipt["capture"]["observedAt"] == "2026-09-12T12:00:00Z"
            assert receipt["acquisitionStartedAt"] == stream.metadata.acquisition_started_at
    proof.write_text(json.dumps({
        "verdict": "pass", "providerVersion": version("spicy-docs"), "providerWheelSha256": wheel_sha,
        "firstMatches": len(summary["matches"]["first"]), "laterMatches": len(summary["matches"]["later"]),
        "xmlCapturedBytes": len(original), "freshCheckoutImports": True, "networkForbidden": True,
        "providerTextCalls": len(text_calls), "closedBeforeReprocessing": True,
        "unavailableSelectionPreservesStatus": True, "refusedTextRetainsBody": True,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
