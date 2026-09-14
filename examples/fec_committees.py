"""Build a metadata catalog from a pinned, retained FEC committee census.

The default is a two-committee authored offline example. Existing releases need
explicit source and blob roots, an artifact pin, and an accepted source verifier.
No API requests, document acquisition, or processing runs happen here.
"""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from rulespec_artifacts import ArtifactPin, LocalBlobSource, LocalMemberSource, Producer
from spicy_docs.source_native import SourceNativeReleaseBuild, SourceNativeReleasePublisher, SourceNativeReleaseReader
from spicy_docs.sources.fec.profile import (
    FEC_COMMITTEE_CENSUS_PROFILE as PROFILE,
    committee_census_scope,
    iter_retained_committee_pages,
)
from spicy_docs.storage.blobs import LocalSourceNativeBlobStore

from docspec.domain.identity import sha256_digest
from docspec.runtime import build_local_catalog, open_local_catalog
from docspec.source_catalog import SuppliedRecordCatalogPolicy, SuppliedRecordSource

NAMESPACE = "urn:docspec:example:fec-committees"
FIXTURES = Path(__file__).with_name("fec_fixtures")
VERSION_MEANING = "caller-retained response observation; not an FEC-issued committee or document revision"


@cache
def _implementation_digest() -> str:
    # The shared artifact format requires an implementation digest. Hash this
    # small example once; source record revisions reuse existing evidence IDs.
    return sha256_digest(Path(__file__).read_bytes())


def catalog_producer() -> Producer:
    implementation = NAMESPACE + ":" + _implementation_digest()
    return Producer("docspec-example", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)


def source_description(reader: SourceNativeReleaseReader) -> dict:
    return {
        "logicalId": reader.pin.logical_id, "artifactDigest": reader.pin.artifact_digest,
        "sourceSystemId": reader.source_system_id, "sourceSystemVersion": reader.source_system_version,
        "sourceStateScope": reader.source_state_scope, "sourceStateDigest": reader.source_state_digest,
        "sourceNativeSchemaSetDigest": reader.source_native_schema_set_digest,
        "collectionOutcome": reader.collection_outcome,
    }


def build_committee_catalog(reader: SourceNativeReleaseReader, workspace: Path, *,
                            max_records: int, max_bytes: int, max_scratch_bytes: int):
    """Retain admitted source facts and join ordered record/evidence streams once.

    The provider admits the source; SuppliedRecordSource owns the in-memory
    record/canonical-byte bounds. Evidence bytes stay at their source location.
    """
    if (reader.source_system_id, reader.source_system_version) != (
        PROFILE.source_system_id, PROFILE.source_system_version,
    ):
        raise ValueError("the committee example requires the FEC committee census source profile")
    description = source_description(reader)
    outcome = description["collectionOutcome"]
    if outcome["recordOutcome"] not in {"empty", "no-record-rejections"}:
        raise ValueError("the committee example requires a source without rejected records")
    compact = {key: value for key, value in description.items() if key != "collectionOutcome"}
    compact["collectionOutcome"] = {
        key: value for key, value in outcome.items() if key == "recordOutcome" or key.endswith("Count")
    }

    def records():
        count = 0
        with closing(reader.iter_renditions()) as renditions:
            if next(renditions, None) is not None:
                raise ValueError("the committee metadata example cannot discard supplied renditions")
        with closing(reader.iter_records()) as rows, closing(reader.iter_record_evidence()) as evidence:
            for row, observation in zip(rows, evidence, strict=True):
                identifier = row["sourceRecordId"]
                if identifier != observation["sourceRecordId"] or observation["failure"] is not None:
                    raise ValueError("source records and successful evidence must have identical ordered membership")
                native = row["record"]["metadata"]
                if native["committee_id"] != identifier:
                    raise ValueError("source committee identity differs from the retained metadata")
                yield {
                    "recordId": reader.source_system_id + ":" + identifier,
                    # The provider's evidence ZIP pins the response and capture
                    # facts. Committees on the same page share this revision.
                    "sourceIssuedVersion": observation["evidenceBlobRef"],
                    "title": native.get("name") or None,
                    "metadata": {"source": compact, "record": dict(row), "evidence": dict(observation),
                                 "versionMeaning": VERSION_MEANING},
                    "candidateRenditions": [],
                }
                count += 1
        if count != outcome["publishedRecordCount"]:
            raise ValueError("catalog input count differs from the published source count")

    supplied = SuppliedRecordSource(records(), source_system_id=NAMESPACE, source_system_version="1",
        source_state_scope=reader.source_state_scope, max_records=max_records, max_bytes=max_bytes)
    return build_local_catalog((supplied,), workspace, policy=SuppliedRecordCatalogPolicy(NAMESPACE, "1"),
        catalog_id=NAMESPACE + ":catalog", producer=catalog_producer(), max_scratch_bytes=max_scratch_bytes)


def publish_fixture(output: Path, *, empty: bool = False) -> dict:
    """Publish authored response bytes through the provider's ordinary public API."""
    raw = (FIXTURES / "committees.json").read_bytes()
    if empty:
        value = json.loads(raw)
        value["results"] = []
        value["pagination"].update(count=0, pages=0)
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    blobs = LocalSourceNativeBlobStore(output / "source-blobs")
    digest = sha256_digest(raw)
    blobs.put_blob(digest, len(raw), (raw,))
    captures = [{
        "requestUrl": "https://api.open.fec.gov/v1/committees/?cycle=2026&sort=committee_id&per_page=100",
        "observedAt": "2026-09-12T12:00:00Z", "responseSha256": digest, "byteSize": len(raw),
        "mediaType": "application/json", "via": "authored offline fixture; no request made",
    }]
    # Identify this authored fixture driver. The repository's wheel qualification
    # checks installed provider bytes; the example does not rescan the package.
    implementation = NAMESPACE + ":fixture-driver:" + _implementation_digest()
    producer = Producer("spicy-docs", implementation, "urn:spicy-regs:source-native-release-verifier", "2.0",
                        implementation)
    published = SourceNativeReleasePublisher(PROFILE, blob_store=blobs,
        clock=lambda: datetime(2026, 9, 12, 12, 1, tzinfo=UTC)).publish(
            iter_retained_committee_pages(captures, blob_source=blobs),
            build=SourceNativeReleaseBuild(query_scope=committee_census_scope(captures), producer=producer,
                                          started_at="2026-09-12T12:00:00Z"),
            destination=output / "source")
    return {"source_root": published.root, "blob_root": blobs.root,
            "logical_id": published.artifact.pin.logical_id, "artifact_digest": published.artifact.pin.artifact_digest,
            "source_implementation_id": implementation}


def run_example(output: Path, *, source_root: Path | None = None, blob_root: Path | None = None,
                logical_id: str | None = None, artifact_digest: str | None = None,
                source_implementation_id: str | None = None, empty: bool = False,
                max_records: int = 50_000, max_bytes: int = 256 * 1024**2,
                max_scratch_bytes: int = 2 * 1024**3) -> dict:
    if not output.is_absolute() or output.exists():
        raise ValueError("output must be an absolute path that does not exist")
    inputs = {"source_root": source_root, "blob_root": blob_root, "logical_id": logical_id,
              "artifact_digest": artifact_digest, "source_implementation_id": source_implementation_id}
    existing = any(value is not None for value in inputs.values())
    if existing and (empty or any(value is None for value in inputs.values())):
        raise ValueError("an existing source requires both roots, both artifact pin fields, and its accepted verifier")
    if not existing:
        inputs = publish_fixture(output, empty=empty)
    reader = SourceNativeReleaseReader(LocalMemberSource(inputs["source_root"]),
        blob_source=LocalBlobSource(inputs["blob_root"]), profile=PROFILE,
        expected_pin=ArtifactPin(inputs["logical_id"], inputs["artifact_digest"]),
        accepted_verifier_implementation_ids=frozenset({inputs["source_implementation_id"]}))
    workspace = Path(output / "dataset")
    built = build_committee_catalog(reader, workspace, max_records=max_records, max_bytes=max_bytes,
                                   max_scratch_bytes=max_scratch_bytes)
    catalog = open_local_catalog(built.reference, workspace, producer=catalog_producer())
    summary = {
        "scope": "pinned retained committee census" if existing else "authored offline committee census",
        "synthetic": not existing, "source": source_description(reader),
        "input": {key: str(value) for key, value in inputs.items()},
        "catalog": built.reference.to_dict(), "catalogProducer": catalog_producer().as_dict(),
        "itemCount": catalog.summary.item_count, "dispositions": dict(catalog.summary.disposition_counts),
        "bounds": {"maxRecords": max_records, "maxCanonicalSuppliedBytes": max_bytes,
                   "maxScratchBytes": max_scratch_bytes},
        "versionMeaning": VERSION_MEANING,
        "limits": ["Only the admitted requests and filters; neither a frozen FEC snapshot nor historical coverage.",
                   "Original response evidence remains in the pinned source release and blob store.",
                   "Committee metadata has no document candidates and is unavailable for body processing.",
                   "The supplied-record byte bound excludes additional Python object memory."],
    }
    (output / "fec-example-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                                                       encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--empty", action="store_true", help="use an authored empty census")
    for name in ("source-root", "blob-root"):
        parser.add_argument("--" + name, type=Path)
    for name in ("logical-id", "artifact-digest", "source-implementation-id"):
        parser.add_argument("--" + name)
    parser.add_argument("--max-records", type=int, default=50_000)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024**2)
    parser.add_argument("--max-scratch-bytes", type=int, default=2 * 1024**3)
    try:
        result = run_example(**vars(parser.parse_args()))
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
