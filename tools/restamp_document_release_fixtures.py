#!/usr/bin/env python3
"""Deterministic builder for the sealed DocumentRelease v2 fixture corpus.

Moved under REF-048 from `tools/build_document_release_fixtures.py` at source
commit c584a1d9fcb89fb8c4253b5bb6879741b0e24c1c. The bundle layout, the
mutation catalogue, and the restamping rules are that file's; the paths and the
schema generation are DocSpec's.

One valid bundle, then one invalid bundle per diagnostic code. Each invalid
bundle is the valid bundle copied and mutated in exactly one way, with every
downstream digest, count, coverage figure, and identity restamped, so the case
violates the rule it is named for and nothing else.

Every byte offset in the fixture is DERIVED from the fixture's own bytes rather
than hand-written. Hand-written offsets in a corpus about offsets would test the
author's arithmetic instead of the validator.

Two inputs this builder needs did not come across with it, so neither mode runs
here yet:

* `SOURCE_CATALOG_FIXTURE` -- the sealed SourceCatalogRelease v1 bundle the
  corpus pins and draws its source items from. It is a different artifact
  family and was not part of this move.
* The packaged schema generation. REF-048 re-homed every schema ``$id``, so a
  clean rebuild stamps `urn:docspec:` identifiers and the packaged filenames,
  while the committed corpus was sealed under the predecessor generation. A
  rebuild therefore MINTS A NEW CORPUS rather than reproducing the sealed one,
  and `--check` would report that difference as drift. Re-minting is a decision
  about the wire contract, not a mechanical refresh, so it is gated behind
  `--allow-regeneration` rather than being the default.

Usage:
  uv run python tools/restamp_document_release_fixtures.py --check
  uv run python tools/restamp_document_release_fixtures.py --allow-regeneration
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from docspec.adapters.document_release_verify import (
    FORMAT,
    FORMAT_VERSION,
    REPRESENTATION_MEDIA_TYPE,
    SCHEMA_FILES,
    SCHEMA_IDS,
    derive_counts,
    derive_coverage,
    mapping_digest,
    stamp_root,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    source_set_digest,
    tree_digest,
    write_canonical_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "document_release_v2"
CORPUS_FILE = FIXTURE_ROOT / "corpus.json"

CORPUS_ID = "urn:docspec:document-corpus:us-federal-register"
PUBLISHED_AT = "2026-08-11T00:00:00Z"

# The exact SourceCatalogRelease v1 fixture this corpus is built from. Read from
# that sealed bundle rather than restated, so the two candidates cannot drift
# apart silently. It did not come across with this builder; until it does, both
# modes stop at `_require_inputs` rather than half-building a corpus.
SOURCE_CATALOG_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "source_catalog_release_v1" / "valid"


def _require_inputs() -> None:
    """Refuse to build unless every sealed input this corpus derives from is here."""

    missing = [
        path
        for path in (
            SOURCE_CATALOG_FIXTURE / "release.json",
            SOURCE_CATALOG_FIXTURE / "data" / "source-items.json",
        )
        if not path.is_file()
    ]
    if missing:
        raise SystemExit(
            "cannot build the DocumentRelease v2 corpus: the sealed "
            "SourceCatalogRelease v1 fixture it pins is absent (missing "
            + ", ".join(path.relative_to(REPO_ROOT).as_posix() for path in missing)
            + ")"
        )

PROCESSING_POLICY = {
    "extractorId": "docspec-visible-text-extractor",
    "maxSegmentBytes": 512,
    "policyId": "urn:docspec:processing-policy:federal-register-html-v1",
    "policySha256": canonical_sha256(
        {"maxSegmentBytes": 512, "splitOn": "structural-node", "stripMarkup": True}
    ),
    "policyVersion": "1.0",
    "segmenterId": "docspec-structural-segmenter",
}


def _catalog_pin() -> dict[str, Any]:
    root = json.loads((SOURCE_CATALOG_FIXTURE / "release.json").read_text(encoding="utf-8"))
    return {
        "releaseDigest": "sha256:" + file_sha256(SOURCE_CATALOG_FIXTURE / "release.json"),
        "releaseId": root["releaseId"],
        "requestedUniverseSetDigest": root["content"]["requestedUniverseSetDigest"],
        "selectedSourceSetDigest": root["content"]["selectedSourceSetDigest"],
    }


def _catalog_items() -> list[dict[str, Any]]:
    return json.loads(
        (SOURCE_CATALOG_FIXTURE / "data" / "source-items.json").read_text(encoding="utf-8")
    )


# ─── The documents, as blocks of visible text ──────────────────────────
#
# Each block becomes a structural node. A block with `heading` True is a
# heading node; blocks nested under it become its children. `excluded` marks
# visible text deliberately not searchable, which lands in the exclusion
# ledger instead of a segment.

DOCUMENT_BLOCKS: dict[str, list[dict[str, Any]]] = {
    "FR-2026-03227": [
        {"kind": "heading", "text": "Salmonella Framework for Raw Poultry Products", "depth": 0},
        {"kind": "heading", "text": "SUMMARY", "depth": 0},
        {
            "kind": "paragraph",
            "text": "The Food Safety and Inspection Service is establishing a framework to reduce Salmonella illnesses attributable to raw poultry products.",
            "depth": 1,
        },
        {"kind": "heading", "text": "SUPPLEMENTARY INFORMATION", "depth": 0},
        {
            "kind": "paragraph",
            "text": "The Agency received 1,204 comments from consumer advocacy organizations, trade associations, and individual commenters.",
            "depth": 1,
        },
        {
            "kind": "table",
            "text": "Table 1 | Establishment | Category | Rate |",
            "depth": 1,
            "excluded": True,
            "reasonCode": "policy.tabular-layout-not-search-text",
            "reason": "A pipe-delimited layout table carries no sentence-level meaning and is excluded from search under the processing policy.",
        },
    ],
    "FR-2026-04188": [
        {
            "kind": "heading",
            "text": "Air Plan Approval; Pennsylvania; Regional Haze Progress Report",
            "depth": 0,
        },
        {
            "kind": "paragraph",
            "text": "The Environmental Protection Agency proposes to approve a state implementation plan revision submitted by the Commonwealth of Pennsylvania.",
            "depth": 1,
        },
    ],
}


def _build_document_bytes(document_id: str) -> dict[str, Any]:
    """Lay out one document's representation and rendition, deriving all offsets."""

    blocks = DOCUMENT_BLOCKS[document_id]
    representation_parts: list[str] = []
    rendition_parts: list[str] = ["<!DOCTYPE html>\n<html><body>\n"]
    laid_out: list[dict[str, Any]] = []
    representation_cursor = 0
    for index, block in enumerate(blocks):
        text = block["text"]
        line = text + "\n"
        encoded = line.encode("utf-8")
        start = representation_cursor
        end = start + len(encoded)
        representation_parts.append(line)
        representation_cursor = end

        tag = "h1" if block["kind"] == "heading" else "p"
        prefix = "".join(rendition_parts)
        opening = f"<{tag}>"
        rendition_start = len(prefix.encode("utf-8")) + len(opening.encode("utf-8"))
        rendition_end = rendition_start + len(text.encode("utf-8"))
        rendition_parts.append(f"{opening}{text}</{tag}>\n")

        laid_out.append(
            {
                **block,
                "index": index,
                "representationStart": start,
                "representationEnd": end,
                "renditionStart": rendition_start,
                "renditionEnd": rendition_end,
            }
        )
    rendition_parts.append("</body></html>\n")
    return {
        "blocks": laid_out,
        "representation": "".join(representation_parts).encode("utf-8"),
        "rendition": "".join(rendition_parts).encode("utf-8"),
    }


def _structural_nodes(document_id: str, version_id: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the source-derived node tree: depth-1 blocks hang off the last heading.

    A section node spans its whole section — its own heading line through the
    end of its last child — not just the heading line. A child's range must lie
    inside its parent's, and a heading that covered only its own text would
    contain nothing, which is both false about the document and unusable as a
    heading path.
    """

    nodes: list[dict[str, Any]] = []
    root_ordinal = 0
    current_parent: str | None = None
    child_ordinal = 0
    for position, block in enumerate(blocks):
        node_id = f"{version_id}#n{block['index']}"
        span_end = block["representationEnd"]
        if block["depth"] == 0:
            parent_id: str | None = None
            ordinal = root_ordinal
            root_ordinal += 1
            current_parent = node_id
            child_ordinal = 0
            for following in blocks[position + 1 :]:
                if following["depth"] == 0:
                    break
                span_end = following["representationEnd"]
        else:
            parent_id = current_parent
            ordinal = child_ordinal
            child_ordinal += 1
        nodes.append(
            {
                "depth": block["depth"],
                "documentVersionId": version_id,
                "headingText": block["text"] if block["kind"] == "heading" else None,
                "nodeKind": block["kind"],
                "ordinal": ordinal,
                "representationEnd": span_end,
                "representationStart": block["representationStart"],
                "structuralNodeId": node_id,
                "structuralParentId": parent_id,
            }
        )
    return nodes


def _heading_path_for(node_id: str, nodes: list[dict[str, Any]]) -> list[str]:
    index = {node["structuralNodeId"]: node for node in nodes}
    chain: list[str] = []
    current = index.get(node_id)
    while current is not None:
        if current["headingText"]:
            chain.append(current["headingText"])
        parent = current["structuralParentId"]
        current = index.get(parent) if parent is not None else None
    chain.reverse()
    return chain


def _search_segments(
    version_id: str,
    blocks: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    rendition_sha256: str,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    ordinal = 0
    for block in blocks:
        if block.get("excluded"):
            continue
        node_id = f"{version_id}#n{block['index']}"
        segments.append(
            {
                "documentVersionId": version_id,
                "evidence": {
                    "coordinateSystem": "rendition-utf8-byte",
                    "end": block["renditionEnd"],
                    "renditionSha256": rendition_sha256,
                    "start": block["renditionStart"],
                },
                "headingPath": _heading_path_for(node_id, nodes),
                "ordinal": ordinal,
                "representationEnd": block["representationEnd"],
                "representationStart": block["representationStart"],
                "segmentId": f"{version_id}#s{ordinal}",
                "structuralParentId": node_id,
            }
        )
        ordinal += 1
    return segments


def _member(bundle: Path, object_key: str, *, role: str, record_count: int | None, schema_id: str, media_type: str) -> dict[str, Any]:
    path = bundle / object_key
    return {
        "byteSize": path.stat().st_size,
        "mediaType": media_type,
        "objectKey": object_key,
        "recordCount": record_count,
        "role": role,
        "schemaId": schema_id,
        "sha256": file_sha256(path),
    }


def _restamp(bundle: Path, state: dict[str, Any]) -> None:
    """Rewrite every derived value in a bundle from its current member bytes."""

    dispositions = state["dispositions"]
    documents = state["documents"]
    nodes = state["nodes"]
    segments = state["segments"]

    (bundle / "data").mkdir(parents=True, exist_ok=True)
    write_canonical_json(bundle / "data" / "source-dispositions.json", dispositions)
    write_canonical_json(bundle / "data" / "documents.json", documents)
    write_canonical_json(bundle / "data" / "structural-nodes.json", nodes)
    write_canonical_json(bundle / "data" / "search-segments.json", segments)

    members = [
        _member(
            bundle,
            "data/source-dispositions.json",
            role="source-dispositions",
            record_count=len(dispositions),
            schema_id=SCHEMA_IDS["source-dispositions"],
            media_type="application/json",
        ),
        _member(
            bundle,
            "data/documents.json",
            role="documents",
            record_count=len(documents),
            schema_id=SCHEMA_IDS["documents"],
            media_type="application/json",
        ),
        _member(
            bundle,
            "data/structural-nodes.json",
            role="structural-nodes",
            record_count=len(nodes),
            schema_id=SCHEMA_IDS["structural-nodes"],
            media_type="application/json",
        ),
        _member(
            bundle,
            "data/search-segments.json",
            role="search-segments",
            record_count=len(segments),
            schema_id=SCHEMA_IDS["search-segments"],
            media_type="application/json",
        ),
    ]
    for role in sorted(SCHEMA_FILES):
        members.append(
            _member(
                bundle,
                f"schemas/{SCHEMA_FILES[role].name}",
                role="schema",
                record_count=None,
                schema_id=SCHEMA_IDS[role],
                media_type="application/schema+json",
            )
        )
    for document in documents:
        members.append(
            _member(
                bundle,
                document["capture"]["objectKey"],
                role="rendition",
                record_count=None,
                schema_id=document["capture"]["mediaType"],
                media_type=document["capture"]["mediaType"],
            )
        )
        members.append(
            _member(
                bundle,
                document["representation"]["objectKey"],
                role="representation",
                record_count=None,
                schema_id=REPRESENTATION_MEDIA_TYPE,
                media_type=REPRESENTATION_MEDIA_TYPE,
            )
        )
    members.sort(key=lambda member: member["objectKey"])

    manifest = {
        "counts": {
            "memberCount": len(members),
            "totalByteSize": sum(member["byteSize"] for member in members),
            "totalRecordCount": sum(member["recordCount"] or 0 for member in members),
        },
        "format": "spicy-artifact-member-manifest",
        "formatVersion": "1.0",
        "manifestId": "global:global",
        "members": members,
        "scope": {"id": "global", "kind": "global"},
    }
    manifest_key = "manifests/global.json"
    write_canonical_json(bundle / manifest_key, manifest)

    schemas = sorted(
        (
            {
                "roles": [role],
                "schemaId": SCHEMA_IDS[role],
                "schemaSha256": file_sha256(bundle / f"schemas/{SCHEMA_FILES[role].name}"),
            }
            for role in SCHEMA_FILES
        ),
        key=lambda descriptor: descriptor["schemaId"],
    )
    selected_ids = [
        row["sourceItemId"] for row in dispositions if row["catalogDisposition"] == "selected"
    ]
    version_ids = [document["documentVersionId"] for document in documents]
    segment_ids = [segment["segmentId"] for segment in segments]
    pairs = [[document["sourceItemId"], document["documentVersionId"]] for document in documents]

    catalog = dict(state["catalog"])
    catalog["selectedSourceSetDigest"] = source_set_digest(selected_ids)
    content = {
        "corpusId": CORPUS_ID,
        "counts": derive_counts(
            dispositions,
            documents,
            nodes,
            segments,
            member_count=len(members),
            total_member_byte_size=sum(member["byteSize"] for member in members),
        ),
        "coverage": derive_coverage(dispositions, documents, segments),
        "documentVersionSetDigest": source_set_digest(version_ids),
        "globalManifest": {
            "byteSize": (bundle / manifest_key).stat().st_size,
            "manifestId": "global:global",
            "objectKey": manifest_key,
            "scopeId": "global",
            "scopeKind": "global",
            "sha256": file_sha256(bundle / manifest_key),
        },
        "joinReceipt": {
            "documentVersionCount": len(version_ids),
            "mappingDigest": mapping_digest(pairs),
            "receiptId": "urn:docspec:join-receipt:source-to-document-v1",
            "selectedSourceItemCount": len(selected_ids),
        },
        "processingPolicy": PROCESSING_POLICY,
        "schemaSet": {
            "schemaSetId": f"urn:spicy:schema-set:v1:{canonical_sha256(schemas)}",
            "schemas": schemas,
        },
        "segmentSetDigest": source_set_digest(segment_ids),
        "selectedSourceSetDigest": source_set_digest(selected_ids),
        "sourceCatalog": catalog,
        "sourceDocumentMappingDigest": mapping_digest(pairs),
    }
    root = {
        "annotations": {
            "buildRunId": "document-release-v2-conformance-fixture",
            "publishedAt": PUBLISHED_AT,
            "releaseStatus": "fixture",
        },
        "content": content,
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
    }
    write_canonical_json(bundle / "release.json", stamp_root(root))


def build_valid_bundle(bundle: Path) -> dict[str, Any]:
    """Materialize the sealed valid bundle and return its record state."""

    _require_inputs()
    if bundle.exists():
        shutil.rmtree(bundle)
    (bundle / "schemas").mkdir(parents=True)
    for role, path in SCHEMA_FILES.items():
        shutil.copyfile(path, bundle / "schemas" / path.name)

    catalog = _catalog_pin()
    items = {item["sourceItemId"]: item for item in _catalog_items()}

    dispositions: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []

    for item in _catalog_items():
        disposition = item["selection"]["disposition"]
        row: dict[str, Any] = {
            "catalogDisposition": disposition,
            "documentId": item["documentId"],
            "documentVersionId": None,
            "processingFailures": [],
            "sourceIssuedVersion": item["sourceIssuedVersion"],
            "sourceItemId": item["sourceItemId"],
        }
        if disposition != "selected":
            row["reason"] = item["selection"]["reason"]
            row["reasonCode"] = item["selection"]["reasonCode"]
        else:
            row["documentVersionId"] = f"{item['documentId']}@{item['sourceIssuedVersion']}"
        dispositions.append(row)

    for row in dispositions:
        if row["catalogDisposition"] != "selected":
            continue
        document_id = row["documentId"]
        version_id = row["documentVersionId"]
        item = items[row["sourceItemId"]]
        laid_out = _build_document_bytes(document_id)

        rendition_key = f"blobs/{document_id}.html"
        representation_key = f"text/{document_id}.txt"
        (bundle / "blobs").mkdir(parents=True, exist_ok=True)
        (bundle / "text").mkdir(parents=True, exist_ok=True)
        (bundle / rendition_key).write_bytes(laid_out["rendition"])
        (bundle / representation_key).write_bytes(laid_out["representation"])
        rendition_sha = file_sha256(bundle / rendition_key)

        rendition = next(
            candidate
            for candidate in item["candidateRenditions"]
            if candidate["mediaType"] == "text/html"
        )
        normalized = item["normalizedMetadata"]
        documents.append(
            {
                "capture": {
                    "byteSize": len(laid_out["rendition"]),
                    "candidateRenditionId": rendition["renditionId"],
                    "catalogReleaseId": catalog["releaseId"],
                    # The fixture's captured bytes are the fixture's own; the
                    # catalog's pre-known digest described the live document, so
                    # only a null expectation can be honest here. The
                    # `expected-digest-mismatch` case exercises the non-null path.
                    "expectedSha256": None,
                    "mediaType": "text/html",
                    "objectKey": rendition_key,
                    "sha256": rendition_sha,
                },
                "documentId": document_id,
                "documentVersionId": version_id,
                "excludedRanges": [
                    {
                        "end": block["representationEnd"],
                        "reason": block["reason"],
                        "reasonCode": block["reasonCode"],
                        "start": block["representationStart"],
                    }
                    for block in laid_out["blocks"]
                    if block.get("excluded")
                ],
                "representation": {
                    "byteSize": len(laid_out["representation"]),
                    "encoding": "utf-8",
                    "mediaType": REPRESENTATION_MEDIA_TYPE,
                    "objectKey": representation_key,
                    "representationId": f"{version_id}#representation",
                    "sha256": file_sha256(bundle / representation_key),
                },
                "sourceIssuedVersion": row["sourceIssuedVersion"],
                "sourceItemId": row["sourceItemId"],
                "sourceMetadata": {
                    "agencies": normalized["agencies"],
                    "catalogReleaseId": catalog["releaseId"],
                    "docketIds": normalized["docketIds"],
                    "documentType": normalized["documentType"],
                    "publicationDate": normalized["publicationDate"],
                    "regulationIdentifierNumbers": normalized["regulationIdentifierNumbers"],
                    "sourceUrl": normalized["sourceUrl"],
                    "title": normalized["title"],
                },
            }
        )
        document_nodes = _structural_nodes(document_id, version_id, laid_out["blocks"])
        nodes.extend(document_nodes)
        segments.extend(
            _search_segments(version_id, laid_out["blocks"], document_nodes, rendition_sha)
        )

    state = {
        "catalog": catalog,
        "dispositions": dispositions,
        "documents": documents,
        "nodes": nodes,
        "segments": segments,
    }
    _restamp(bundle, state)
    return state


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _state(bundle: Path) -> dict[str, Any]:
    root = _load(bundle / "release.json")
    return {
        "catalog": root["content"]["sourceCatalog"],
        "dispositions": _load(bundle / "data" / "source-dispositions.json"),
        "documents": _load(bundle / "data" / "documents.json"),
        "nodes": _load(bundle / "data" / "structural-nodes.json"),
        "segments": _load(bundle / "data" / "search-segments.json"),
    }


def build_corpus(fixture_root: Path = FIXTURE_ROOT) -> list[dict[str, Any]]:
    """Rebuild every bundle and return the sealed corpus rows."""

    valid = fixture_root / "valid"
    invalid_root = fixture_root / "invalid"
    build_valid_bundle(valid)
    if invalid_root.exists():
        shutil.rmtree(invalid_root)
    invalid_root.mkdir(parents=True)

    cases: list[dict[str, Any]] = [
        {
            "bundle": "valid",
            "expectedCode": "valid",
            "expectedPath": None,
            "name": "valid",
            "treeSha256": tree_digest(valid),
        }
    ]

    def copy_case(name: str) -> Path:
        target = invalid_root / name
        shutil.copytree(valid, target)
        return target

    def record(name: str, code: str, path: str | None, bundle: Path) -> None:
        cases.append(
            {
                "bundle": f"invalid/{name}",
                "expectedCode": code,
                "expectedPath": path,
                "name": name,
                "treeSha256": tree_digest(bundle),
            }
        )

    bundle = copy_case("noncanonical-root")
    (bundle / "release.json").write_bytes((bundle / "release.json").read_bytes() + b"\n")
    record("noncanonical-root", "invalid.root-syntax", "release.json", bundle)

    bundle = copy_case("unknown-version")
    root = _load(bundle / "release.json")
    root["formatVersion"] = "2.1"
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record("unknown-version", "invalid.format", "release.json", bundle)

    bundle = copy_case("wrong-identity")
    root = _load(bundle / "release.json")
    root["releaseId"] = "urn:docspec:document-release:v2:" + "0" * 64
    write_canonical_json(bundle / "release.json", root)
    record("wrong-identity", "invalid.identity", "release.json/releaseId", bundle)

    bundle = copy_case("unsafe-path")
    manifest = _load(bundle / "manifests" / "global.json")
    for member in manifest["members"]:
        if member["role"] == "search-segments":
            member["objectKey"] = "../escaped-search-segments.json"
    manifest["members"].sort(key=lambda member: member["objectKey"])
    write_canonical_json(bundle / "manifests" / "global.json", manifest)
    root = _load(bundle / "release.json")
    root["content"]["globalManifest"]["byteSize"] = (bundle / "manifests" / "global.json").stat().st_size
    root["content"]["globalManifest"]["sha256"] = file_sha256(bundle / "manifests" / "global.json")
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record("unsafe-path", "invalid.path", "manifests/global.json/members/0/objectKey", bundle)

    bundle = copy_case("missing-member")
    (bundle / "text" / "FR-2026-04188.txt").unlink()
    record("missing-member", "invalid.membership-missing", "text/FR-2026-04188.txt", bundle)

    bundle = copy_case("extra-member")
    (bundle / "undeclared.json").write_bytes(b"{}")
    record("extra-member", "invalid.membership-extra", "undeclared.json", bundle)

    bundle = copy_case("member-digest")
    (bundle / "data" / "structural-nodes.json").write_bytes(
        (bundle / "data" / "structural-nodes.json").read_bytes() + b" "
    )
    record("member-digest", "invalid.member-digest", "data/structural-nodes.json", bundle)

    bundle = copy_case("unknown-node-kind")
    state = _state(bundle)
    state["nodes"][0]["nodeKind"] = "chapter"
    _restamp(bundle, state)
    record(
        "unknown-node-kind",
        "invalid.schema",
        "data/structural-nodes.json/0/nodeKind",
        bundle,
    )

    bundle = copy_case("duplicate-segment")
    state = _state(bundle)
    state["segments"][1]["segmentId"] = state["segments"][0]["segmentId"]
    _restamp(bundle, state)
    record(
        "duplicate-segment",
        "invalid.duplicate-identity",
        "data/search-segments.json/1/segmentId",
        bundle,
    )

    bundle = copy_case("catalog-pin-mismatch")
    root = _load(bundle / "release.json")
    root["content"]["sourceCatalog"]["releaseId"] = (
        "urn:spicy-regs:source-catalog-release:v1:" + "0" * 64
    )
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record(
        "catalog-pin-mismatch",
        "invalid.source-catalog-pin",
        "data/documents.json/0/capture/catalogReleaseId",
        bundle,
    )

    bundle = copy_case("missing-projection-reason")
    state = _state(bundle)
    index = next(
        i for i, row in enumerate(state["dispositions"]) if row["catalogDisposition"] == "excluded"
    )
    del state["dispositions"][index]["reason"]
    _restamp(bundle, state)
    record(
        "missing-projection-reason",
        "invalid.disposition",
        f"data/source-dispositions.json/{index}/reason",
        bundle,
    )

    bundle = copy_case("expected-digest-mismatch")
    state = _state(bundle)
    state["documents"][0]["capture"]["expectedSha256"] = "sha256:" + "0" * 64
    _restamp(bundle, state)
    record(
        "expected-digest-mismatch",
        "invalid.capture",
        "data/documents.json/0/capture/expectedSha256",
        bundle,
    )

    bundle = copy_case("representation-bytes-differ")
    state = _state(bundle)
    key = state["documents"][0]["representation"]["objectKey"]
    (bundle / key).write_bytes((bundle / key).read_bytes().replace(b"Salmonella", b"SALMONELLA"))
    _restamp(bundle, state)
    record(
        "representation-bytes-differ",
        "invalid.representation",
        "data/documents.json/0/representation/sha256",
        bundle,
    )

    bundle = copy_case("orphan-structural-parent")
    state = _state(bundle)
    state["nodes"][2]["structuralParentId"] = f"{state['nodes'][2]['documentVersionId']}#missing"
    _restamp(bundle, state)
    record(
        "orphan-structural-parent",
        "invalid.structure",
        "data/structural-nodes.json/2/structuralParentId",
        bundle,
    )

    bundle = copy_case("segment-heading-path")
    state = _state(bundle)
    state["segments"][2]["headingPath"] = ["Wrong Heading"]
    _restamp(bundle, state)
    record(
        "segment-heading-path",
        "invalid.segment",
        "data/search-segments.json/2/headingPath",
        bundle,
    )

    bundle = copy_case("coverage-gap")
    state = _state(bundle)
    # Drop one document's exclusion ledger entry: its visible text is then
    # neither segmented nor excluded, which is the hole the PLAN forbids.
    state["documents"][0]["excludedRanges"] = []
    _restamp(bundle, state)
    record(
        "coverage-gap",
        "invalid.coverage",
        "data/documents.json/0/representation",
        bundle,
    )

    bundle = copy_case("join-not-one-to-one")
    root = _load(bundle / "release.json")
    root["content"]["joinReceipt"]["selectedSourceItemCount"] += 1
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record(
        "join-not-one-to-one",
        "invalid.join",
        "release.json/content/joinReceipt/selectedSourceItemCount",
        bundle,
    )

    bundle = copy_case("segment-set-digest")
    root = _load(bundle / "release.json")
    root["content"]["segmentSetDigest"] = "sha256:" + "0" * 64
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record(
        "segment-set-digest",
        "invalid.set-digest",
        "release.json/content/segmentSetDigest",
        bundle,
    )

    bundle = copy_case("counts-mismatch")
    root = _load(bundle / "release.json")
    root["content"]["counts"]["structuralNodeCount"] += 1
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record("counts-mismatch", "invalid.counts", "release.json/content/counts", bundle)

    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="rebuild into a scratch tree and fail on any drift"
    )
    parser.add_argument(
        "--allow-regeneration",
        action="store_true",
        help="replace the committed corpus, accepting the schema generation this build stamps",
    )
    args = parser.parse_args(argv)

    if args.check:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "document-release-v2"
            scratch.mkdir()
            rebuilt = canonical_json_bytes({"cases": build_corpus(scratch)})
        if rebuilt != CORPUS_FILE.read_bytes():
            print("DRIFT: corpus.json differs from a clean rebuild")
            return 1
        print("document-release-v2 fixtures match a clean rebuild")
        return 0

    if not args.allow_regeneration:
        print(
            "refusing to rebuild in place: this builder stamps the packaged schema "
            "generation, so it would replace the sealed corpus rather than reproduce "
            "it. Pass --allow-regeneration to re-mint the corpus deliberately."
        )
        return 1

    cases = build_corpus()
    write_canonical_json(CORPUS_FILE, {"cases": cases})
    print(f"wrote {len(cases)} sealed cases to {CORPUS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
