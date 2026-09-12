"""Catalog one explicit bill version, fetch XML, and compare two lexical processors.

Requires an installed SpicyDocs bill-acquisition wheel. Uses synthetic HTTP
responses unless --live is supplied. The output directory must not exist.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from rulespec_artifacts import Producer
from spicy_docs.sources.congress.bill_acquisition import BillAcquirer, BillAcquisitionBudget
from spicy_docs.sources.congress.bill_status import BillIdentity, bill_status_locator, bill_xml_locator, select_bill_xml

from docspec.domain.identity import canonical_json_bytes, identity_digest, sha256_digest
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import RetryPolicy
from docspec.domain.processors import ProcessorResourceIdentity, ProcessorResourceKind
from docspec.domain.references import BlobRef
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import build_local_catalog, open_local_catalog, open_local_inspection, prepare_local_experiment
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from examples.govinfo_bill_fetcher import BillContentFetcher, capture_facts, provider_installation, retain_refusal
from examples.phrase_match_processor import PhraseMatchProcessor

FIXTURES = Path(__file__).with_name("bill_fixtures")
FIXTURE_IDENTITY = BillIdentity(119, "hr", 6028)
FIXTURE_TIME = datetime(2026, 9, 12, 12, tzinfo=UTC)
BILL_HEADINGS = {"header": 2}


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fixture_transport(requests: list[str]) -> httpx.MockTransport:
    """Use real provider parsing and acquisition with authored response bytes."""
    responses = {
        bill_status_locator(FIXTURE_IDENTITY): FIXTURES / "status.xml",
        bill_xml_locator(FIXTURE_IDENTITY, "BILLS-119hr6028ih"): FIXTURES / "introduced.xml",
        bill_xml_locator(FIXTURE_IDENTITY, "BILLS-119hr6028eh"): FIXTURES / "engrossed.xml",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if request.method != "GET" or url not in responses:
            raise AssertionError(f"unexpected fixture request: {request.method} {url}")
        return httpx.Response(200, stream=httpx.ByteStream(responses[url].read_bytes()),
                              headers={"Content-Type": "application/xml"})

    return httpx.MockTransport(respond)


def _processor(revision: str, phrases: tuple[str, ...], retry: RetryPolicy, output: Path) -> PhraseMatchProcessor:
    raw = canonical_json_bytes({"terms": [{"id": phrase.replace(" ", "-"), "label": phrase,
                                            "phrases": [phrase]} for phrase in phrases]})
    (output / f"phrases-{revision}.json").write_bytes(raw)
    return PhraseMatchProcessor(ProcessorResourceIdentity(
        "urn:docspec:example:bill-phrases", ProcessorResourceKind.REFERENCE_DATA, revision, sha256_digest(raw),
    ), raw, retry_policy=retry)


def _finish(prepared, workspace, source_producer, release_producer, name: str, output: Path):
    run = prepared.run()
    view = open_local_inspection(prepared.plan, workspace, document_release_producer=release_producer,
                                 source_catalog_producer=source_producer, run_ref=run)
    report = {
        "plan": prepared.plan.to_dict(), "run": run.to_dict(), "result": None,
        "handoff": prepared.handoff_ref.to_dict(), "inspection": view.summary(),
    }
    _write(output / f"{name}.json", report)
    failures = list(view.records("failures"))
    if failures:
        _write(output / f"{name}-failures.json", failures)
        raise RuntimeError(f"{name} recorded {len(failures)} failure(s); inspect {output / f'{name}-failures.json'} "
                           f"and {output / 'source-evidence'}")
    result = prepared.retain(run)
    view = open_local_inspection(prepared.plan, workspace, document_release_producer=release_producer,
                                 source_catalog_producer=source_producer, release_ref=result)
    _write(output / f"{name}.json", report | {"result": result.to_dict(), "inspection": view.summary()})
    return result, view


def _matches(view, processor: PhraseMatchProcessor) -> list[dict]:
    return [match for row in view.records("derived:" + processor.description.processor_id)
            for match in row["payload"]["value"]["matches"]]


def run_example(output: Path, *, package_id: str, identity: BillIdentity = FIXTURE_IDENTITY,
                live: bool = False) -> dict:
    """Keep catalog choice, captured bytes, and processor results separately inspectable."""
    if not output.is_absolute() or output.exists():
        raise ValueError("output must be an absolute path that does not exist")
    if not live and identity != FIXTURE_IDENTITY:
        raise ValueError("offline fixtures describe only 119/hr/6028")
    provider = provider_installation()
    budget = BillAcquisitionBudget(max_requests=2, max_status_bytes=2 * 1024**2, max_text_bytes=2 * 1024**2,
                                   timeout_seconds=20, min_request_interval_seconds=1 if live else 0)
    requests: list[str] = []
    transport = None if live else fixture_transport(requests)
    clock = (lambda: datetime.now(UTC)) if live else (lambda: FIXTURE_TIME)
    output.mkdir(parents=True)
    evidence = output / "source-evidence"
    evidence.mkdir()
    with BillAcquirer(budget=budget, transport=transport, clock=clock) as acquirer:
        try:
            status = acquirer.acquire_status(identity)
        except Exception as error:
            retain_refusal(error, evidence / "bill-status-refusal.json")
            raise
        (evidence / "bill-status.xml").write_bytes(status.capture.body)
        status_receipt = {"capture": capture_facts(status.capture), "requestCount": status.request_count,
                          "budget": asdict(status.budget), "provider": provider, "synthetic": not live}
        _write(evidence / "bill-status-acquisition.json", status_receipt)
        try:
            selected, rendition = select_bill_xml(status.status, package_id)
        except Exception as error:
            retain_refusal(error, evidence / "bill-selection-refusal.json")
            raise
        namespace = "urn:docspec:example:govinfo-bills"
        source = SuppliedRecordSource(({
            "recordId": f"{identity.congress}/{identity.bill_type}/{identity.number}",
            "sourceIssuedVersion": package_id + ":" + status.capture.sha256,
            "title": status.status.title,
            "metadata": {"billStatus": asdict(status.status), "statusAcquisition": status_receipt,
                         "selectedPackageId": package_id, "selectedTextVersion": asdict(selected)},
            "candidateRenditions": [SourceCatalogCandidate(
                "selected-bill-xml", "application/xml", "source-url", rendition.url,
            ).to_dict()],
        },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
            max_records=1, max_bytes=4 * 1024**2)
        implementation = "urn:docspec:example:govinfo-bills:" + identity_digest({
            "provider": provider, "example": sha256_digest(Path(__file__).read_bytes()),
            "fetcher": sha256_digest(Path(__file__).with_name("govinfo_bill_fetcher.py").read_bytes()),
            "processor": sha256_digest(Path(__file__).with_name("phrase_match_processor.py").read_bytes()),
        })
        source_producer = Producer("docspec-example", implementation,
                                  "urn:docspec:verifier:source-catalog", "1.0.0", implementation)
        release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
        workspace = LocalWorkspace(output)
        catalog = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
            catalog_id="urn:docspec:example:govinfo-bills:catalog", producer=source_producer,
            max_scratch_bytes=16 * 1024**2)
        rows = list(open_local_catalog(catalog.reference, workspace, producer=source_producer).iter_mappings())
        _write(output / "catalog-preview.json", {"reference": catalog.reference.to_dict(), "items": rows})
        retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0)
        first = _processor("1", ("public access",), retry, output)
        second = _processor("2", ("public access", "machine readable"), retry, output)
        fetcher = BillContentFetcher(acquirer, status, package_id, evidence, clock=clock)
        settings = {
            "limits": WorkLimits(1, 2 * 1024**2, 500, 500, 1000, 32 * 1024**2, 120, 1),
            "source_catalog_producer": source_producer, "document_release_producer": release_producer,
            "deadline_epoch_seconds": 4_000_000_000,
            "content_fetcher": fetcher, "retry_policy": retry,
            "extractor": VisibleTextExtractor(xml_heading_levels=BILL_HEADINGS),
            "segmenter": VisibleTextBlockSegmenter(),
        }
        run_time = clock().isoformat().replace("+00:00", "Z")
        with prepare_local_experiment(catalog.reference, workspace, processors=(first,),
                                      completed_at=run_time, **settings) as prepared:
            base, initial = _finish(prepared, workspace, source_producer, release_producer, "processed", output)

    # The provider client is now closed. This independent run must reuse the
    # retained capture/representation/segments when only its phrases change.
    run_time = clock().isoformat().replace("+00:00", "Z")
    with prepare_local_experiment(catalog.reference, workspace, processors=(second,), base_release=base,
                                  completed_at=run_time,
                                  **settings) as prepared:
        retained, later = _finish(prepared, workspace, source_producer, release_producer, "reprocessed", output)
    layers = ("files", "representations", "segments")
    preserved = all([row["payload"] for row in initial.records(kind)] ==
                    [row["payload"] for row in later.records(kind)] for kind in layers)
    captured = list(later.records("files"))[0]["payload"]
    original = b"".join(later.read_blob(BlobRef.from_dict(captured["blob"]), max_bytes=2 * 1024**2))
    representation = list(later.records("representations"))[0]["payload"]
    visible = b"".join(later.read_blob(BlobRef.from_dict(representation["blob"]), max_bytes=2 * 1024**2))
    counts = later.summary()["work"]["counts"]
    if not preserved or any(counts[key] for key in ("newCapturedFiles", "newRepresentations", "newSegments")):
        raise AssertionError("processor-only iteration repeated or changed upstream work")
    summary = {
        "scope": "one explicitly selected bill version; synthetic responses" if not live else
                 "one explicitly selected bill version; bounded live observation",
        "provider": provider, "selectedPackageId": package_id,
        "offeredTextVersions": len(status.status.text_versions),
        "offeredFormats": sum(len(version.formats) for version in status.status.text_versions),
        "selectedXmlUrl": rendition.url, "capturedSha256": captured["blob"]["digest"],
        "capturedByteSize": len(original), "visibleText": visible.decode("utf-8"),
        "matches": {"first": _matches(initial, first), "later": _matches(later, second)},
        "retainedResult": retained.to_dict(), "originalLayersPreserved": preserved,
        "reprocessingNewCaptureCount": counts["newCapturedFiles"],
        "fixtureRequests": requests if not live else None,
        "interpretation": "literal phrase occurrences, not legal meaning or applicability",
    }
    _write(output / "bill-example-summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-id", required=True, help="exact offered BILLS package; never inferred as latest")
    parser.add_argument("--congress", type=int, default=119)
    parser.add_argument("--bill-type", default="hr")
    parser.add_argument("--number", type=int, default=6028)
    parser.add_argument("--live", action="store_true", help="make bounded public GovInfo requests")
    args = parser.parse_args()
    try:
        result = run_example(args.output, package_id=args.package_id,
                             identity=BillIdentity(args.congress, args.bill_type, args.number), live=args.live)
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
