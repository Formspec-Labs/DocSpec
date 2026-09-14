"""Build and revise an experiment over one explicitly selected annual CFR section.

Authored offline responses are the default. --live enables two bounded public
GovInfo requests: volume MODS metadata, then its offered annual section XML.
"""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from rulespec_artifacts import Producer
from spicy_docs.sources.cfr.acquisition import CfrAcquirer, CfrAcquisitionBudget
from spicy_docs.sources.cfr.annual import annual_cfr_xml_locator
from spicy_docs.sources.cfr.edition import annual_cfr_edition_locator
from spicy_docs.sources.cfr.models import AnnualCfrSelection

from docspec.domain.identity import identity_digest, sha256_digest
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import CoreWorkspace, build_local_catalog, open_local_catalog
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.domain.source_catalog import SourceCatalogItem
from docspec.domain.core_admission import record_value
from examples.dataset_example_support import capture_facts, run_documents, matches, output_value, phrase_processor, retain_refusal, write_json
from examples.govinfo_cfr_fetcher import AnnualCfrContentFetcher, select_section
from examples.provider_identity import provider_installation

FIXTURES = Path(__file__).with_name("cfr_fixtures")
FIXTURE_SELECTION = AnnualCfrSelection(2025, 1, 1, "18.1")
FIXTURE_TIME = datetime(2026, 9, 12, 12, tzinfo=UTC)
CFR_HEADINGS = {"SUBJECT": 2}
NAMESPACE = "urn:docspec:example:annual-cfr"
MAX_BYTES = 2 * 1024**2


def fixture_transport(requests: list[str]) -> httpx.MockTransport:
    responses = {
        annual_cfr_edition_locator(replace(FIXTURE_SELECTION, section=None)): FIXTURES / "edition.xml",
        annual_cfr_xml_locator(FIXTURE_SELECTION): FIXTURES / "section.xml",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if request.method != "GET" or url not in responses:
            raise AssertionError(f"unexpected fixture request: {request.method} {url}")
        return httpx.Response(200, stream=httpx.ByteStream(responses[url].read_bytes()),
                              headers={"Content-Type": "application/xml"})

    return httpx.MockTransport(respond)


def run_example(output: Path, *, selection: AnnualCfrSelection = FIXTURE_SELECTION, live: bool = False) -> dict:
    if not output.is_absolute() or output.exists():
        raise ValueError("output must be an absolute path that does not exist")
    if selection.section is None:
        raise ValueError("this example requires one explicit annual section")
    if not live and replace(selection, section=FIXTURE_SELECTION.section) != FIXTURE_SELECTION:
        raise ValueError("offline fixtures describe only annual 2025/title1/volume1")
    provider = provider_installation()
    budget = CfrAcquisitionBudget(1, MAX_BYTES, 20, 1 if live else 0)
    requests: list[str] = []
    transport = None if live else fixture_transport(requests)
    clock = (lambda: datetime.now(UTC)) if live else (lambda: FIXTURE_TIME)
    output.mkdir(parents=True)
    evidence = output / "source-evidence"
    evidence.mkdir()
    with ExitStack() as views:
        with CfrAcquirer(budget=budget, transport=transport, clock=clock) as acquirer:
            try:
                edition = acquirer.acquire_annual_edition(replace(selection, section=None))
            except Exception as error:
                retain_refusal(error, evidence / "cfr-mods-refusal.json", source="cfr")
                raise
            (evidence / "cfr-mods.xml").write_bytes(edition.capture.body)
            receipt = {"capture": capture_facts(edition.capture), "requestCount": edition.request_count,
                       "budget": asdict(edition.budget), "provider": provider, "synthetic": not live}
            write_json(evidence / "cfr-mods-acquisition.json", receipt)
            try:
                selected, offer = select_section(edition, selection)
            except Exception as error:
                retain_refusal(error, evidence / "cfr-selection-refusal.json", source="cfr")
                raise
            source = SuppliedRecordSource(({
                "recordId": selected.fields("extension", "accessId")[0].text,
                "sourceIssuedVersion": selected.fields("extension", "accessId")[0].text,
                "title": " / ".join(node.text for node in selected.fields("titleInfo", "title")) or selection.section,
                "metadata": {"mods": asdict(edition.metadata), "edition": asdict(edition.edition),
                             "modsAcquisition": receipt, "selection": asdict(selection),
                             "selectedConstituentPath": selected.element.path, "selectedUrlPath": offer.path},
                "candidateRenditions": [SourceCatalogCandidate(
                    "selected-annual-section-xml", "application/xml", "source-url", offer.text,
                ).to_dict()],
            },), source_system_id=NAMESPACE, source_system_version="1", source_state_scope="complete-snapshot",
                max_records=1, max_bytes=16 * 1024**2)
            implementation = NAMESPACE + ":" + identity_digest({
                "provider": provider,
                "exampleFiles": {name: sha256_digest(Path(__file__).with_name(name).read_bytes()) for name in (
                    "govinfo_cfr.py", "govinfo_cfr_fetcher.py", "dataset_example_support.py", "phrase_match_processor.py",
                )},
            })
            source_producer = Producer("docspec-example", implementation,
                                      "urn:docspec:verifier:source-catalog", "1.0.0", implementation)
            workspace = views.enter_context(CoreWorkspace(output))
            catalog = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(NAMESPACE, "1"),
                catalog_id=NAMESPACE + ":catalog", producer=source_producer, max_scratch_bytes=64 * 1024**2)
            rows = list(open_local_catalog(catalog.reference, workspace, producer=source_producer).iter_mappings())
            write_json(output / "catalog-preview.json", {"reference": catalog.reference.to_dict(), "items": rows})
            first = phrase_processor("1", ("public access",), output, resource_id=NAMESPACE + ":phrases")
            second = phrase_processor("2", ("public access", "machine readable"), output, resource_id=NAMESPACE + ":phrases")
            pipeline = workspace.documents(fetcher=AnnualCfrContentFetcher(acquirer, edition, selection, evidence, clock),
                extractor=VisibleTextExtractor(xml_heading_levels=CFR_HEADINGS), segmenter=VisibleTextBlockSegmenter())
            source_state = pipeline.import_sources((SourceCatalogItem.from_dict(row) for row in rows), state_id="source-catalog")
            _, initial_documents = run_documents(workspace, pipeline, source_state.state_id,
                name="processed", processors=(first,), output=output)

        # Reprocessing after the acquisition client closes reuses retained stages.
        retained, later_documents = run_documents(workspace, pipeline, source_state.state_id,
            name="reprocessed", processors=(second,), output=output)
        initial, later = initial_documents[0][1], later_documents[0][1]
        preserved = initial[:3] == later[:3]
        captured = output_value(workspace, later[0], "capture")
        original = output_value(workspace, later[0], "content")
        visible = output_value(workspace, later[1], "content")
        counts = {"newCapturedFiles": int(initial[0] != later[0]), "newRepresentations": int(initial[1] != later[1]),
                  "newSegments": int(initial[2] != later[2])}
        new_work = counts
        if not preserved or any(counts.values()):
            raise AssertionError("processor-only iteration repeated or changed upstream work")
        summary = {
            "scope": "one explicitly selected annual section; " +
                     ("bounded live observation" if live else "synthetic responses"),
            "provider": provider, "selection": asdict(selection), "selectedXmlUrl": offer.text,
            "metadataConstituentCount": len(edition.metadata.constituents),
            "metadataUrlCount": sum(len(record.urls) for record in edition.metadata.constituents),
            "edition": asdict(edition.edition), "capturedSha256": captured["blob"]["digest"],
            "capturedByteSize": len(original), "visibleText": visible.decode("utf-8"),
            "matches": {"first": matches(workspace, pipeline, "processed"), "later": matches(workspace, pipeline, "reprocessed")},
            "retainedResult": record_value(retained), "originalLayersPreserved": preserved,
            "reprocessingNewWork": new_work, "fixtureRequests": requests if not live else None,
            "interpretation": "document-wide literal phrase occurrences, including XML metadata; no legal interpretation",
        }
        write_json(output / "cfr-example-summary.json", summary)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--title", type=int, default=1)
    parser.add_argument("--volume", type=int, default=1)
    parser.add_argument("--section", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        result = run_example(args.output, selection=AnnualCfrSelection(args.year, args.title, args.volume, args.section),
                             live=args.live)
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
