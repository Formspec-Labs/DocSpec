"""Catalog retained community comment-table facts without fetching attachments.

Run: python -m examples.spicyregs_comments --output /absolute/new-directory
Synthetic Parquet uses the provider's public table profile; no network is used.
"""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO, StringIO
from pathlib import Path

from rulespec_artifacts import Producer
from spicy_docs.cli.source_native import main as source_main
from spicy_docs.schemas.spicy_regs_public_tables import PUBLIC_COMMENT_FILE_COLUMNS
from spicy_docs.source_native.profiles import SPICY_REGS_PUBLIC_COMMENT_PROFILE
from spicy_docs.sources.public_comments.native import PublicTableCapture, comment_partition_locator

from docspec.domain.identity import identity_digest, sha256_digest
from docspec.runtime import build_local_catalog, open_local_catalog, preview_local_catalog
from docspec.source_catalog import (
    SourceCatalogCandidate, SpicyDocsSourceNativeAdapter, SuppliedRecordCatalogPolicy, SuppliedRecordSource,
)
from examples.provider_identity import provider_installation

NAMESPACE = "urn:docspec:example:spicyregs-comments"
DOCKET = "EPA-2026-0001"
MAX_RECORDS = 10
MAX_BYTES = 1024**2
ATTACHMENT = "https://downloads.regulations.gov/EPA-2026-0001-0001/attachment.pdf"


def catalog_producer() -> Producer:
    implementation = NAMESPACE + ":" + sha256_digest(Path(__file__).read_bytes())
    return Producer("docspec-example", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)


def fixture_partition(*, invalid_row: bool = False) -> bytes:
    """Author three small rows in the existing provider schema, including nulls."""
    import polars as pl

    rows = []
    for number in range(1, 4):
        row = dict.fromkeys(PUBLIC_COMMENT_FILE_COLUMNS)
        row.update({"comment_id": f"{DOCKET}-{number:04d}", "docket_id": DOCKET,
                    "document_type": "Public Submission"})
        rows.append(row)
    rows[0].update({"title": "Café access", "comment": "See attached", "modify_date": "2026-09-01T12:00:00Z",
        "attachments_json": json.dumps([{"title": "Supporting comment", "formats": [
            {"url": ATTACHMENT, "format": "pdf", "size": 123},
        ]}])})
    rows[1].update({"comment": "Please preserve café access.", "docket_id": "EPA-2026-0002",
                    "text_content": "Table-supplied extracted text", "text_extraction_status": "success"})
    rows[2].update({"comment": "", "attachments_json": "[broken JSON"})
    if invalid_row:
        rows[1]["comment_id"] = None
    frame = pl.DataFrame({name: [row[name] for row in rows] for name in PUBLIC_COMMENT_FILE_COLUMNS},
                         schema={name: pl.String for name in PUBLIC_COMMENT_FILE_COLUMNS})
    buffer = BytesIO()
    frame.write_parquet(buffer)
    return buffer.getvalue()


def build_comment_catalog(source: SpicyDocsSourceNativeAdapter, workspace: Path):
    """Keep admitted source facts and field evidence in the existing supplied-record format."""
    description = source.describe()
    profile = SPICY_REGS_PUBLIC_COMMENT_PROFILE
    if (description.source_system_id, description.source_system_version) != (
        profile.source_system_id, profile.source_system_version,
    ):
        raise ValueError("the example requires the SpicyRegs public-comment table profile")
    if description.collection_outcome["recordOutcome"] != "no-record-rejections":
        raise ValueError("this example accepts only a nonempty source with no record rejections")
    renditions = {}
    with closing(source.iter_renditions()) as rows:
        for count, row in enumerate(rows, 1):
            if count > 40:
                raise ValueError("the comment example supports at most forty attachment candidates")
            renditions.setdefault(row["sourceRecordId"], []).append(row)

    def records():
        with closing(source.iter_records()) as rows:
            for row in rows:
                native = row["record"]
                offered = renditions.pop(row["sourceRecordId"], [])
                yield {
                    "recordId": row["sourceRecordId"], "sourceIssuedVersion": identity_digest(row),
                    "title": native["title"] or None,
                    "metadata": {"source": description.to_dict(), "record": dict(row), "renditions": offered,
                                 "evidence": source.record_evidence(row["sourceRecordId"])},
                    "candidateRenditions": [SourceCatalogCandidate(
                        item["renditionId"], item["mediaType"], "source-url", item["locator"],
                        expected_sha256=item["expectedSha256"], expected_byte_size=item["expectedByteSize"],
                    ).to_dict() for item in offered],
                }
        if renditions:
            raise ValueError("attachment candidates name a record outside the supplied table")

    supplied = SuppliedRecordSource(records(), source_system_id=NAMESPACE, source_system_version="1",
        source_state_scope=description.source_state_scope, max_records=MAX_RECORDS, max_bytes=MAX_BYTES)
    return build_local_catalog((supplied,), workspace, policy=SuppliedRecordCatalogPolicy(NAMESPACE, "1"),
        catalog_id=NAMESPACE + ":catalog", producer=catalog_producer(), max_scratch_bytes=8 * MAX_BYTES)


def inspect_comments(catalog, docket_id: str) -> dict:
    """Filter exact metadata in the same catalog; candidate selection is a separate fact."""
    if catalog.summary.item_count > MAX_RECORDS:
        raise ValueError("the comment example supports at most ten catalog records")
    items = []
    with closing(catalog.iter_mappings()) as rows:
        for row in rows:
            metadata = row["sourceNativeFacts"][0]["fields"]["metadata"]
            native = metadata["record"]["record"]
            items.append({
                "documentId": row["documentId"], "sourceItemId": row["sourceItemId"],
                "docketMatches": native["docket_id"] == docket_id,
                "unavailableFields": sorted(name for name, value in native.items() if value is None),
                "fieldDiagnostics": metadata["record"]["fieldDiagnostics"],
                "documentCandidates": row["candidateRenditions"], "catalogSelection": row["selection"],
            })
    return {"docketId": docket_id, "comparison": "exact, case-sensitive", "items": items,
            "matchedDocumentIds": [row["documentId"] for row in items if row["docketMatches"]]}


def run_example(output: Path, *, docket_id: str = DOCKET, invalid_row: bool = False) -> dict:
    """Publish the synthetic partition; an invalid row must be refused by the source, never admitted."""
    if not output.is_absolute() or output.exists():
        raise ValueError("output must be an absolute path that does not exist")
    provider = provider_installation()
    provider_implementation = NAMESPACE + ":provider:" + provider["installedFilesSha256"]
    partition = fixture_partition(invalid_row=invalid_row)
    output.mkdir(parents=True)
    (output / "input.parquet").write_bytes(partition)
    release, blobs = output / "source", output / "source-blobs"
    published, refused = StringIO(), StringIO()
    observed = []

    def capture(locator: str) -> PublicTableCapture | None:
        observed.append(locator)
        if locator == comment_partition_locator("EPA", 1):
            return None  # The provider records its contiguous-probing assumption.
        if locator != comment_partition_locator("EPA", 0):
            raise AssertionError("the fixture supplies only one named agency partition")
        return PublicTableCapture(locator, partition, "2026-09-12T00:00:00Z", '"fixture-etag"')

    code = source_main([
        "publish", "--source", "spicy-regs-public-comments", "--agency", "EPA",
        "--destination", str(release), "--blob-store", str(blobs), "--implementation-id", provider_implementation,
    ], fetch_public_table=capture, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC), stdout=published, stderr=refused)
    source_result = json.loads(refused.getvalue() if code else published.getvalue())
    (output / "source-result.json").write_text(json.dumps(source_result, indent=2) + "\n", encoding="utf-8")
    report = {
        "input": "three synthetic SpicyRegs table rows; no live requests", "provider": provider,
        "partitionSha256": sha256_digest(partition), "partitionByteSize": len(partition),
        "fixtureTableProbes": observed, "sourceRoot": str(release), "sourceBlobRoot": str(blobs),
        "sourceResult": source_result, "sourceRecordRejections": None,
        "catalog": None, "preview": None, "commentInspection": None,
        "documentProcessing": {"status": "not requested", "capturedFiles": 0, "capturedBytes": 0},
    }
    if code:
        if not invalid_row or source_result["error"]["code"] != "acquisition-failed" or (
            "row lacks comment_id" not in source_result["error"]["message"]
        ):
            raise RuntimeError(f"source publication failed; inspect {output / 'source-result.json'}")
    else:
        if invalid_row:
            raise RuntimeError("the invalid table row unexpectedly produced an admitted source")
        source = SpicyDocsSourceNativeAdapter.from_local(release, blob_root=blobs,
            logical_id=source_result["logicalId"], artifact_digest=source_result["artifactDigest"],
            profile=SPICY_REGS_PUBLIC_COMMENT_PROFILE,
            accepted_verifier_implementation_ids=frozenset({provider_implementation}))
        with closing(source.iter_failures(limit=MAX_RECORDS)) as failures:
            report["sourceRecordRejections"] = list(failures)
        workspace = Path(output / "dataset")
        result = build_comment_catalog(source, workspace)
        catalog = open_local_catalog(result.reference, workspace, producer=catalog_producer())
        report.update({"catalog": result.reference.to_dict(),
            "preview": preview_local_catalog(result.reference, workspace, producer=catalog_producer(),
                                             sample_limit=MAX_RECORDS, max_sample_bytes=MAX_BYTES),
            "commentInspection": inspect_comments(catalog, docket_id)})
    (output / "spicyregs-comments.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docket", default=DOCKET, help="exact metadata filter; never schedules document work")
    parser.add_argument("--invalid-row", action="store_true", help="show a retained input and source refusal")
    args = parser.parse_args()
    print(json.dumps(run_example(args.output, docket_id=args.docket, invalid_row=args.invalid_row), indent=2))


if __name__ == "__main__":
    main()
