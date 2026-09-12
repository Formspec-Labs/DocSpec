"""Build a catalog of retained GAO page facts and filter their literal topics.

Run: python -m examples.gao_topics --case matching --output /absolute/new-directory
The three source fixtures are synthetic. This example makes no network requests.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from rulespec_artifacts import Producer
from spicy_docs.cli.source_native import main as source_main
from spicy_docs.source_native_profiles import GAO_PRODUCT_PAGE_PROFILE
from spicy_docs.sources.gao.native import gao_product_url
from spicy_docs.sources.zyte import ZyteHttpResponse

from docspec.domain.identity import sha256_digest
from docspec.runtime import build_local_catalog, open_local_catalog
from docspec.source_catalog import SpicyDocsSourceNativeAdapter, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from examples.provider_identity import provider_installation

FIXTURES = Path(__file__).with_name("gao_fixtures")
PRODUCT_ID = "gao-26-107693"
CASES = ("matching", "unexpected", "missing")
TOPIC = "Information Security"
NAMESPACE = "urn:docspec:example:gao-topics"


def catalog_producer() -> Producer:
    implementation = NAMESPACE + ":" + sha256_digest(Path(__file__).read_bytes())
    return Producer("docspec-example", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)


def build_topic_catalog(source: SpicyDocsSourceNativeAdapter, workspace: LocalWorkspace):
    """Map admitted provider records, retaining their source pin and field evidence."""
    description = source.describe()
    if (description.source_system_id, description.source_system_version) != (
        GAO_PRODUCT_PAGE_PROFILE.source_system_id, GAO_PRODUCT_PAGE_PROFILE.source_system_version,
    ):
        raise ValueError("the GAO topic example requires the GAO product-page source profile")

    def records():
        for row in source.iter_records():
            native = row["record"]
            yield {
                "recordId": row["sourceRecordId"], "sourceIssuedVersion": native["htmlSha256"], "title": None,
                "metadata": {"source": description.to_dict(), "record": dict(row),
                             "evidence": source.record_evidence(row["sourceRecordId"])},
                # The provider offers page facts, not report attachment renditions.
                "candidateRenditions": [],
            }

    supplied = SuppliedRecordSource(records(), source_system_id=NAMESPACE, source_system_version="1",
        source_state_scope=description.source_state_scope, max_records=10, max_bytes=1024**2)
    return build_local_catalog((supplied,), workspace, policy=SuppliedRecordCatalogPolicy(NAMESPACE, "1"),
        catalog_id=NAMESPACE + ":catalog", producer=catalog_producer(), max_scratch_bytes=8 * 1024**2)


def topic_selection(catalog, label: str) -> dict:
    """Query this small catalog without changing it or scheduling a processing run."""
    if catalog.summary.item_count > 10:
        raise ValueError("the topic example supports at most ten catalog records")
    items = []
    for row in catalog.iter_mappings():
        metadata = row["sourceNativeFacts"][0]["fields"]["metadata"]
        native = metadata["record"]["record"]
        items.append({
            "documentId": row["documentId"], "publisherTopic": native["publisherTopic"],
            "sourceField": "record.publisherTopic", "sourceHtmlSha256": native["htmlSha256"],
            "source": {key: metadata["source"][key] for key in ("logicalId", "artifactDigest")},
            "evidence": metadata["evidence"],
            "topicMatches": native["publisherTopic"]["label"] == label,
            "documentCandidateCount": len(row["candidateRenditions"]),
        })
    return {"label": label, "comparison": "exact, case-sensitive", "items": items,
            "matchedDocumentIds": [row["documentId"] for row in items if row["topicMatches"]]}


def run_example(output: Path, *, case: str = "matching", label: str = TOPIC) -> dict:
    if case not in CASES:
        raise ValueError(f"case must be one of {CASES}")
    if not output.is_absolute() or output.exists():
        raise ValueError("output must be an absolute path that does not exist")
    provider = provider_installation()
    provider_implementation = NAMESPACE + ":provider:" + provider["installedFilesSha256"]
    body = (FIXTURES / f"{case}.html").read_bytes()
    url = gao_product_url(PRODUCT_ID)
    output.mkdir(parents=True)
    release, blobs = output / "source", output / "source-blobs"
    published, refused = StringIO(), StringIO()

    def capture(requested_url: str) -> ZyteHttpResponse:
        if requested_url != url:
            raise ValueError("the fixture supplies only its explicitly named GAO product")
        return ZyteHttpResponse(url, url, 200, "text/html; charset=UTF-8", body)

    code = source_main([
        "publish", "--source", "gao-product-pages", "--product-id", PRODUCT_ID,
        "--destination", str(release), "--blob-store", str(blobs),
        "--implementation-id", provider_implementation,
    ], fetch_gao=capture, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC), stdout=published, stderr=refused)
    source_result = json.loads(refused.getvalue() if code else published.getvalue())
    (output / "source-result.json").write_text(json.dumps(source_result, indent=2) + "\n", encoding="utf-8")
    report = {"input": "one synthetic GAO page; no live requests", "case": case,
              "inputSha256": sha256_digest(body), "provider": provider, "sourceResult": source_result,
              "sourceRoot": str(release), "sourceBlobRoot": str(blobs), "catalog": None, "topicSelection": None}
    if code:
        # A missing topic is a source refusal, never a fabricated empty catalog.
        if case != "missing" or source_result["error"]["code"] != "acquisition-failed" or (
            "topic anchor" not in source_result["error"]["message"]
        ):
            raise RuntimeError(f"source publication failed; inspect {output / 'source-result.json'}")
    else:
        if case == "missing":
            raise RuntimeError("the missing-topic fixture unexpectedly produced an admitted source")
        source = SpicyDocsSourceNativeAdapter.from_local(release, blob_root=blobs,
            logical_id=source_result["logicalId"], artifact_digest=source_result["artifactDigest"],
            profile=GAO_PRODUCT_PAGE_PROFILE,
            accepted_verifier_implementation_ids=frozenset({provider_implementation}))
        workspace = LocalWorkspace(output / "dataset")
        result = build_topic_catalog(source, workspace)
        catalog = open_local_catalog(result.reference, workspace, producer=catalog_producer())
        report.update({"catalog": result.reference.to_dict(), "topicSelection": topic_selection(catalog, label)})
    (output / "gao-topics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", choices=CASES, default="matching")
    parser.add_argument("--topic", default=TOPIC, help="exact, case-sensitive publisher label to match")
    args = parser.parse_args()
    print(json.dumps(run_example(args.output, case=args.case, label=args.topic), indent=2))


if __name__ == "__main__":
    main()
