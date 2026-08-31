#!/usr/bin/env python3
"""Deterministic builder for the DOCSPEC-generation DocumentRelease v2 corpus.

Moved under REF-048 from `tools/build_document_release_fixtures.py` at source
commit c584a1d9fcb89fb8c4253b5bb6879741b0e24c1c. The bundle layout, the
mutation catalogue, and the restamping rules are that file's; the paths, the
schema generation, and the record shapes below are DocSpec's.

One valid bundle, then one invalid bundle per diagnostic code. Each invalid
bundle is the valid bundle copied and mutated in exactly one way, with every
downstream digest, count, coverage figure, and identity restamped, so the case
violates the rule it is named for and nothing else.

Every byte offset in the fixture is DERIVED from the fixture's own bytes rather
than hand-written. Hand-written offsets in a corpus about offsets would test the
author's arithmetic instead of the validator. The retention floor's observed
minimum is derived the same way, from the fixture's own two documents.

What this builder mints
-----------------------
The DOCSPEC minting generation, at `tests/fixtures/document_release_v2_docspec`
-- `docs/decisions/0001-document-release-2-0.md` restamp items 1 through 16, as
far as they are mechanically specified. The predecessor corpus at
`tests/fixtures/document_release_v2` is NOT rebuilt by this tool and never
should be: it is the frozen regression anchor the generation-aware verifier is
measured against, sealed by the tree digests in its own `corpus.json`.

All sixteen items are implemented. Three of them -- 2, 3, and 7 -- were stopped
on the first pass because item 2 as written was self-contradictory, and the
2026-08-31 amendment to that decision resolved exactly that:

* A1 drops `renditionOrdinal` from the `attachmentId` preimage, so one row can
  name the attachment while its sub-rows name the renditions, and item 2's two
  schemas are mintable. Item 3's 6 -> 8 widening follows.
* A2 names the per-kind fields item 7 required and left unnamed.
* A3 seals `reasonCode` as a bounded kebab-case string, with the enum closure
  recorded as a first-real-mint obligation rather than guessed at now.
* A4 gives the digest-bucketed `text/` and `blobs/` members a `text-body-index`,
  so one body's bytes are recoverable and digest-verifiable from a bucket it
  shares. The refusal to mint a multi-body bucket therefore lifts wherever the
  index covers it, and this builder writes the index for every body in every
  bucket -- including the single-body buckets this corpus happens to produce,
  because an accounting that only appears at scale is an accounting nobody
  tested.

Every one of the three record types this decision seals is MINTED here, and none
is left present-and-empty: amendment B4 landed the attachments and amendment C6
the one comment. A sealed schema nothing has ever written a row against is a
contract nobody has read, and two of the three had been in exactly that state.

Usage:
  uv run python tools/restamp_document_release_fixtures.py --check
  uv run python tools/restamp_document_release_fixtures.py --allow-regeneration
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from rulespec_artifacts import FramedSection, framed_section_digest

from docspec.adapters.document_release_verify import (
    DOCSPEC_GENERATION,
    FORMAT,
    FORMAT_VERSION,
    REPRESENTATION_MEDIA_TYPE,
    SCHEMA_FILES,
    SCHEMA_IDS,
    FRAMED_SET_DOMAINS,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
    TEXT_BODY_SET_DOMAIN,
    TABULAR_MEDIA_TYPES,
    TEXT_BODY_INDEX_ROLE,
    TEXT_BODY_KEYS,
    derive_counts,
    derive_coverage,
    framed_set_digest,
    stamp_root,
    verify_document_release,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_strict_canonical_jsonl,
    tree_digest,
    write_canonical_json,
    write_canonical_jsonl,
)
from docspec.domain.identity import stable_urn
from docspec.domain.storage import partition_bucket
from docspec.processing.retention_floors import (
    NORMALIZED_VISIBLE_TEXT_FRACTION,
    normalized_byte_size,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "document_release_v2_docspec"
CORPUS_FILE = FIXTURE_ROOT / "corpus.json"

# The frozen predecessor corpus. Named so the refusal below can point at it, and
# never written to.
PREDECESSOR_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "document_release_v2"

CORPUS_ID = "urn:docspec:document-corpus:us-federal-register"
PUBLISHED_AT = "2026-08-11T00:00:00Z"

# The text body key and tabular media type this builder mints under. Read from
# the gate's own tables rather than restated, so builder and verifier cannot
# drift apart about what the docspec generation is.
TEXT_BODY_KEY = TEXT_BODY_KEYS[DOCSPEC_GENERATION]
TABULAR_MEDIA_TYPE = TABULAR_MEDIA_TYPES[DOCSPEC_GENERATION]
DOCUMENT_BODY = "document-body"

# Restamp item 11: text and blob members follow the SourceCatalog multipart
# partition pattern, bucketing by digest of `textBodyId`.
PARTITION_BUCKET_COUNT = 64

# Restamp item 14: the acquisition wall clock lives in the capture record and
# outside every preimage. One document carries a start instant and the other
# carries null, so the nullable branch is exercised rather than merely allowed.
ACQUIRED_AT = "2026-08-10T00:00:00Z"
ACQUISITION_STARTED_AT = "2026-08-09T23:59:58Z"

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

# Restamp item 6: one policy record per `(textKind, mediaType)`, each digesting
# the extractor and the segmenter themselves so policy content cannot drift
# under an unchanged id. The bodies are the fixture's, and the digests are taken
# over them rather than written down.
EXTRACTOR_ID = "docspec-visible-text-extractor"
SEGMENTER_ID = "docspec-structural-segmenter"
MAX_SEGMENT_BYTES = 512
EXTRACTOR_BODY = {"extractorId": EXTRACTOR_ID, "stripMarkup": True, "unit": "visible-text"}
SEGMENTER_BODY = {
    "maxSegmentBytes": MAX_SEGMENT_BYTES,
    "segmenterId": SEGMENTER_ID,
    "splitOn": "structural-node",
}

# The declared floor this fixture's extractor had to clear. A constant, because
# a floor that is derived from the corpus it gates would gate nothing; the
# observed minimum beside it IS measured, from the fixture's own bodies, and the
# builder refuses unless `observedMinimum > value` holds.
RETENTION_FLOOR_VALUE = "0.5"
RETENTION_FLOOR_UNIT = NORMALIZED_VISIBLE_TEXT_FRACTION
RETENTION_FLOOR_POPULATION = "document-release-v2-conformance-fixture"
# The kinds this corpus mints text for. A policy per `(textKind, mediaType)` is
# not decoration here: amendment B4's `invalid.retention-floor` refuses a text
# body whose kind and media type no declared policy governs, so a corpus that
# carries an attachment must declare the attachment's floor -- and, since
# amendment C6, a comment's. Amendment C1: the declared `mediaType` is the one a
# capture row carries, matched literally, so this constant is the same string
# every capture in this corpus declares rather than its retention format key.
POLICY_KINDS: tuple[str, ...] = (DOCUMENT_BODY, "attachment", "comment")
POLICY_MEDIA_TYPE = "text/html"

# ─── The one comment (amendment C6) ────────────────────────────────────
#
# Amendment B4 required the sealed valid bundle to "finally CARRY an attachment
# and comments"; it landed the attachment and left `data/comments.jsonl` at zero
# bytes, so one of the three record types this decision sealed had never been
# minted by anything. It is minted here.
#
# `commentId` EQUALS the catalog's comment `sourceRecordId` (`/data/id`), which
# for regulations.gov is a sixteen-hex-digit string, so the fixture's is one
# rather than a URN this producer invented. The selection policy is projected
# verbatim from the sealed upstream one; its digest is a constant because DocSpec
# does not compute it -- DocSpec does not select comments.
COMMENT_ID = "0900006481f0c3a7"
COMMENT_OWNER_DOCUMENT_ID = "FR-2026-03227"
COMMENT_SELECTION_POLICY_DIGEST = "sha256:" + "3c" * 32
COMMENT_MODIFY_DATE = "2026-03-02T18:04:11Z"
# The `invalid/comment-selection` case's second comment, and the policy nobody
# sealed that it claims to have inherited.
TWIN_COMMENT_ID = "0900006481f0c3b2"
TWIN_SELECTION_POLICY_DIGEST = "sha256:" + "9d" * 32


def _decimal_fraction(numerator: int, denominator: int, places: int = 4) -> str:
    """Write one ratio below 1 as the decimal string this format can carry.

    Truncated rather than rounded, so a measured minimum is never reported
    higher than it was, and stripped of trailing zeros so one ratio has one
    spelling. Binary floating point is refused outright by the canonicaliser,
    which is why this is a string at all.
    """

    if numerator <= 0 or numerator >= denominator:
        raise SystemExit(f"retention measurement {numerator}/{denominator} is not a ratio below 1")
    scaled = f"{(numerator * 10**places) // denominator:0{places + 1}d}"
    value = (scaled[:-places] + "." + scaled[-places:]).rstrip("0")
    if value.endswith("."):
        raise SystemExit(f"retention measurement {numerator}/{denominator} truncated to zero")
    return value


def _processing_policies(observed_minimum: str) -> list[dict[str, Any]]:
    """The sorted `(textKind, mediaType)` policy array restamp item 6 requires."""

    if not _greater(observed_minimum, RETENTION_FLOOR_VALUE):
        raise SystemExit(
            f"retention floor {RETENTION_FLOOR_VALUE} has no margin under the observed minimum "
            f"{observed_minimum}: a floor at or above the lowest legitimate document is a future "
            "false refusal"
        )
    return sorted(
        (
            {
                "extractorDigest": canonical_sha256(EXTRACTOR_BODY),
                "extractorId": EXTRACTOR_ID,
                "maxSegmentBytes": MAX_SEGMENT_BYTES,
                "mediaType": POLICY_MEDIA_TYPE,
                "retentionFloor": {
                    "observedMinimum": observed_minimum,
                    "population": RETENTION_FLOOR_POPULATION,
                    "unit": RETENTION_FLOOR_UNIT,
                    "value": RETENTION_FLOOR_VALUE,
                },
                "segmenterDigest": canonical_sha256(SEGMENTER_BODY),
                "segmenterId": SEGMENTER_ID,
                "textKind": text_kind,
            }
            for text_kind in POLICY_KINDS
        ),
        key=lambda policy: (policy["textKind"], policy["mediaType"]),
    )


def _greater(left: str, right: str) -> bool:
    """Compare two decimal-string fractions without going through a float."""

    width = max(len(left), len(right))
    return left.ljust(width, "0") > right.ljust(width, "0")


def _catalog_pin() -> dict[str, Any]:
    """The `{catalogId, catalogDigest}` pin restamp item 9 reshapes it to.

    `catalogDigest` is the BYTE sha256 of the pinned root, exactly as the
    decision requires, taken over those bytes rather than a re-serialisation.

    `catalogId` is DERIVED here, and that is a departure the decision could not
    anticipate: it says the id is "the pinned SourceCatalog snapshot's own
    value, verbatim", but the sealed fixture this corpus pins is a
    SourceCatalogRelease v1 bundle whose own `catalogId` is
    `urn:spicy-regs:source-catalog:us-federal-register` -- a series name that
    cannot match the `urn:docspec:source-catalog:v1:<64 hex>` pattern the same
    item requires. Verbatim and the pattern are not both satisfiable against
    this input. So the id is derived from the pinned items under the catalog's
    OWN state domain, the same way the decision derives
    `selectedSourceSetDigest` from a pin that does not carry it: same algorithm,
    same domain, recomputable by anyone holding the same pinned bytes.
    """

    items = _catalog_items()
    state = framed_section_digest(
        "docspec-source-catalog-state/1",
        (FramedSection("sourceItems", len(items), items),),
    )
    return {
        "catalogDigest": file_sha256(SOURCE_CATALOG_FIXTURE / "release.json"),
        "catalogId": "urn:docspec:source-catalog:v1:" + state.split(":", 1)[1],
    }


def _catalog_items() -> list[dict[str, Any]]:
    return json.loads(
        (SOURCE_CATALOG_FIXTURE / "data" / "source-items.json").read_text(encoding="utf-8")
    )


def _partition_key(prefix: str, text_body_id: str) -> str:
    """Bucket one text body's member by digest of its `textBodyId` (item 11)."""

    return f"{prefix}/{partition_bucket(text_body_id, PARTITION_BUCKET_COUNT):04d}"


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


# One enumerated attachment carries its own text. The blocks are the fixture's,
# laid out by the same machinery a document body is: an attachment is a text
# body under the document body's rules unchanged -- one text pipeline, three
# kinds -- so a corpus that lays it out differently would be testing two
# pipelines and proving neither.
DOCUMENT_BLOCKS["FR-2026-04188-appendix"] = [
    {"kind": "heading", "text": "Appendix A: Monitoring Network Summary", "depth": 0},
    {
        "kind": "paragraph",
        "text": "The Commonwealth operates fourteen visibility monitoring sites under the Interagency Monitoring of Protected Visual Environments network.",
        "depth": 1,
    },
]


# The one comment's own text, laid out by the same machinery again: a comment is
# a text body under the document body's rules unchanged, so a corpus that laid it
# out differently would be testing two pipelines and proving neither.
DOCUMENT_BLOCKS["comment-" + COMMENT_ID] = [
    {"kind": "heading", "text": "Comment of the National Poultry Council", "depth": 0},
    {
        "kind": "paragraph",
        "text": "The proposed framework sets a performance standard our members cannot meet without capital that the compliance period does not allow them to raise.",
        "depth": 1,
    },
]


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


def _structural_nodes(
    document_id: str,
    body_id: str,
    blocks: list[dict[str, Any]],
    text_kind: str = DOCUMENT_BODY,
) -> list[dict[str, Any]]:
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
        node_id = f"{body_id}#n{block['index']}"
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
                "headingText": block["text"] if block["kind"] == "heading" else None,
                "nodeKind": block["kind"],
                "ordinal": ordinal,
                "representationEnd": span_end,
                "representationStart": block["representationStart"],
                "structuralNodeId": node_id,
                "structuralParentId": parent_id,
                TEXT_BODY_KEY: body_id,
                "textKind": text_kind,
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
    body_id: str,
    blocks: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    rendition_sha256: str,
    text_kind: str = DOCUMENT_BODY,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    ordinal = 0
    for block in blocks:
        if block.get("excluded"):
            continue
        node_id = f"{body_id}#n{block['index']}"
        segments.append(
            {
                # Restamp item 15 reserves `evidenceGrade` on this coordinate
                # and leaves it unpopulated in 2.0, so no 2.0 row writes it.
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
                "segmentId": f"{body_id}#s{ordinal}",
                "structuralParentId": node_id,
                TEXT_BODY_KEY: body_id,
                "textKind": text_kind,
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


DATA_MEMBERS: tuple[tuple[str, str], ...] = (
    ("source-dispositions", "dispositions"),
    ("documents", "documents"),
    # Restamp item 2, both now carrying rows: the attachments since amendment
    # B4, the comment since amendment C6.
    ("attachments", "attachments"),
    ("comments", "comments"),
    ("structural-nodes", "nodes"),
    ("search-segments", "segments"),
)

# Amendment A4's index over partitioned member bytes. Its rows are governed by
# the member-manifest schema's own `textBodyIndexRow` `$def`, so the schema set
# stays at the eight restamp item 3 fixes it at.
TEXT_BODY_INDEX_KEY = "manifests/text-body-index.jsonl"


def _unique(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """One record per distinct key, first occurrence wins.

    Every `/2` domain is a SET digest over unique keys, and the framed digest
    refuses a repeated one outright rather than folding it away. A mutation that
    duplicates an identity therefore cannot be written at all unless the builder
    digests the SET the rows denote -- which is what a set digest of those rows
    is. The duplicate stays a defect: `invalid.duplicate-identity` reports it,
    and orders ahead of `invalid.set-digest`, so the case still fails for the
    rule it is named for.
    """

    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(row[field], row)
    return list(seen.values())


def _set_digest(domain: str, rows: list[dict[str, Any]]) -> str:
    """The domain's digest over the SET these rows denote.

    Amendment B1's guard: a `/3` digest refuses a repeated key rather than
    absorbing it, so a bundle mutated to carry a duplicate identity has no
    computable set digest at all. This builder still has to write one, and the
    honest value is the digest of the set the rows denote -- so the duplicate
    case reports `invalid.duplicate-identity` for the rule it breaks AND
    `invalid.set-digest`, because a release carrying a repeated identity cannot
    truthfully name its own members. Both are recorded in the case's expected
    diagnostics rather than hidden by a deduplicating digest.
    """

    key = FRAMED_SET_DOMAINS[domain].key
    return framed_set_digest(domain, _unique(list(rows), key))


def _bucket_counts(index_rows: list[dict[str, Any]], family: str) -> dict[str, int]:
    """How many text bodies share each partition member, keyed by object key."""

    counts: dict[str, int] = {}
    for row in index_rows:
        if row["family"] != family:
            continue
        counts[row["member"]] = counts.get(row["member"], 0) + 1
    return counts


def _slice_digest(bundle: Path, row: dict[str, Any]) -> str:
    """Digest exactly the member bytes one text body owns."""

    raw = (bundle / row["member"]).read_bytes()
    return hashlib.sha256(raw[row["startByte"] : row["startByte"] + row["byteLength"]]).hexdigest()


def _restamp(bundle: Path, state: dict[str, Any]) -> None:
    """Rewrite every derived value in a bundle from its current member bytes."""

    dispositions = state["dispositions"]
    documents = state["documents"]
    attachments = state["attachments"]
    comments = state["comments"]
    nodes = state["nodes"]
    segments = state["segments"]
    rows_by_role = {
        "source-dispositions": dispositions,
        "documents": documents,
        "attachments": attachments,
        "comments": comments,
        "structural-nodes": nodes,
        "search-segments": segments,
    }

    # Amendment A4. The layout -- which member a body's bytes lie in, and where
    # -- is a structural fact carried in the state; the slice DIGEST is derived,
    # and is re-taken here from the member's current bytes like every other
    # derived value in this builder.
    index_rows = [
        {**row, "sha256": _slice_digest(bundle, row)} for row in state["textBodyIndex"]
    ]
    write_canonical_jsonl(bundle / TEXT_BODY_INDEX_KEY, index_rows)

    # Restamp item 11: tabular members are JSONL, one canonical-JSON record per
    # newline-terminated line, following `docspec-record-layer/1.1`.
    members: list[dict[str, Any]] = []
    for role, _ in DATA_MEMBERS:
        object_key = f"data/{role}.jsonl"
        write_canonical_jsonl(bundle / object_key, rows_by_role[role])
        members.append(
            _member(
                bundle,
                object_key,
                role=role,
                record_count=len(rows_by_role[role]),
                schema_id=SCHEMA_IDS[role],
                media_type=TABULAR_MEDIA_TYPE,
            )
        )
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
    members.append(
        _member(
            bundle,
            TEXT_BODY_INDEX_KEY,
            role=TEXT_BODY_INDEX_ROLE,
            record_count=len(index_rows),
            schema_id=SCHEMA_IDS["member-manifest"],
            media_type=TABULAR_MEDIA_TYPE,
        )
    )
    # Restamp items 11 and 16: one member per partition bucket, each carrying
    # the number of text bodies it holds rather than a null. The count comes off
    # the index, which knows every body in every bucket regardless of kind.
    rendition_counts = _bucket_counts(index_rows, "blob")
    representation_counts = _bucket_counts(index_rows, "text")
    captures = [
        *(document["capture"] for document in documents),
        *(comment["capture"] for comment in comments),
        *(
            rendition["capture"]
            for attachment in attachments
            for rendition in attachment["renditions"]
            if rendition["capture"] is not None
        ),
    ]
    for object_key, count in sorted(rendition_counts.items()):
        media_type = next(
            capture["mediaType"] for capture in captures if capture["objectKey"] == object_key
        )
        members.append(
            _member(
                bundle,
                object_key,
                role="rendition",
                record_count=count,
                schema_id=media_type,
                media_type=media_type,
            )
        )
    for object_key, count in sorted(representation_counts.items()):
        members.append(
            _member(
                bundle,
                object_key,
                role="representation",
                record_count=count,
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
    joined = _unique(
        [
            {
                "documentId": document["documentId"],
                "documentVersionId": document["documentVersionId"],
                "sourceItemId": document["sourceItemId"],
            }
            for document in documents
        ],
        "sourceItemId",
    )
    text_bodies = _unique(
        [
            {TEXT_BODY_KEY: row[TEXT_BODY_KEY], "textKind": row["textKind"]}
            for row in [*documents, *attachments, *comments]
            if row.get(TEXT_BODY_KEY) is not None
        ],
        TEXT_BODY_KEY,
    )
    mapping = framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, joined)
    # Amendment B6: derived over the PINNED catalog's items, never projected
    # from this release's own rows. The fixture's pinned catalog carries six
    # items and this release selects two, so the two values differ -- which is
    # the point: a consumer holding the pinned bytes reproduces this one, and
    # could not reproduce a projection of rows it does not have.
    pin_selected = framed_set_digest(
        SELECTED_SOURCE_SET_DOMAIN,
        [
            {"documentId": item["documentId"], "sourceItemId": item["sourceItemId"]}
            for item in _catalog_items()
        ],
    )

    content = {
        # Amendment B1: every row-typed digest frames its members' FULL LOGICAL
        # ROWS under a `/3` domain. The rows go in as this bundle carries them;
        # `framed_set_digest` applies the exclusion set, so a fixture and the
        # gate cannot disagree about what a `/3` digest covers.
        "attachmentSetDigest": _set_digest("docspec-attachment-set/3", attachments),
        "commentSetDigest": _set_digest("docspec-comment-set/3", comments),
        "corpusId": CORPUS_ID,
        "counts": derive_counts(
            dispositions,
            documents,
            nodes,
            segments,
            member_count=len(members),
            total_member_byte_size=sum(member["byteSize"] for member in members),
            attachments=attachments,
            comments=comments,
            generation=DOCSPEC_GENERATION,
        ),
        "coverage": derive_coverage(
            dispositions,
            documents,
            segments,
            key=TEXT_BODY_KEY,
            attachments=attachments,
            comments=comments,
        ),
        "documentVersionSetDigest": _set_digest("docspec-document-version-set/3", documents),
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
            "mappingDigest": mapping,
            "receiptId": "urn:docspec:join-receipt:source-to-document-v1",
            "selectedSourceItemCount": len(selected_ids),
        },
        "processingPolicies": state["processingPolicies"],
        "schemaSet": {
            "schemaSetId": f"urn:spicy:schema-set:v1:{canonical_sha256(schemas)}",
            "schemas": schemas,
        },
        "segmentSetDigest": _set_digest("docspec-segment-set/3", segments),
        "selectedSourceSetDigest": pin_selected,
        "sourceCatalog": dict(state["catalog"]),
        "sourceDispositionSetDigest": _set_digest("docspec-source-disposition-set/3", dispositions),
        "sourceDocumentMappingDigest": mapping,
        "structuralNodeSetDigest": _set_digest("docspec-structural-node-set/3", nodes),
        "textBodySetDigest": framed_set_digest(TEXT_BODY_SET_DOMAIN, text_bodies),
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
    retention_ratios: list[tuple[int, int]] = []
    # Restamp item 11's partition buckets, accumulated rather than written one
    # file per body, and amendment A4's index over them. `_place` appends one
    # body's bytes to its bucket and records the slice, so a bucket holding
    # several bodies stays recoverable byte for byte.
    partitions: dict[str, bytearray] = {}
    index_rows: list[dict[str, Any]] = []

    def _place(family: str, prefix: str, body_id: str, payload: bytes) -> tuple[str, str]:
        object_key = _partition_key(prefix, body_id)
        bucket = partitions.setdefault(object_key, bytearray())
        start = len(bucket)
        bucket.extend(payload)
        digest = hashlib.sha256(payload).hexdigest()
        index_rows.append(
            {
                "byteLength": len(payload),
                "family": family,
                "member": object_key,
                "sha256": digest,
                "startByte": start,
                "textBodyId": body_id,
            }
        )
        return object_key, digest

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

        # Restamp item 11: bucket by digest of the text body id, one member per
        # bucket. `textBodyId` EQUALS `documentVersionId` for a document body.
        body_id = version_id
        rendition_key, rendition_sha = _place("blob", "blobs", body_id, laid_out["rendition"])
        representation_key, representation_sha = _place(
            "text", "text", body_id, laid_out["representation"]
        )
        # Amendment B5: both sides whitespace-normalized, so the fixture's
        # declared unit is true of the number beside it.
        retention_ratios.append(
            (
                normalized_byte_size(laid_out["representation"]),
                normalized_byte_size(laid_out["rendition"]),
            )
        )

        rendition = next(
            candidate
            for candidate in item["candidateRenditions"]
            if candidate["mediaType"] == "text/html"
        )
        normalized = item["normalizedMetadata"]
        documents.append(
            {
                "capture": {
                    # Restamp item 14: the wall clock lives here and in no
                    # preimage. The second document carries a null start, so the
                    # nullable branch is exercised rather than merely allowed.
                    "acquiredAt": ACQUIRED_AT,
                    "acquisitionStartedAt": (
                        ACQUISITION_STARTED_AT if not documents else None
                    ),
                    "byteSize": len(laid_out["rendition"]),
                    "candidateRenditionId": rendition["renditionId"],
                    "catalogReleaseId": catalog["catalogId"],
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
                    "sha256": representation_sha,
                },
                "sourceIssuedVersion": row["sourceIssuedVersion"],
                "sourceItemId": row["sourceItemId"],
                "sourceMetadata": {
                    "agencies": normalized["agencies"],
                    "catalogReleaseId": catalog["catalogId"],
                    "docketIds": normalized["docketIds"],
                    "documentType": normalized["documentType"],
                    "publicationDate": normalized["publicationDate"],
                    "regulationIdentifierNumbers": normalized["regulationIdentifierNumbers"],
                    "sourceUrl": normalized["sourceUrl"],
                    "title": normalized["title"],
                },
                TEXT_BODY_KEY: body_id,
                "textKind": DOCUMENT_BODY,
            }
        )
        document_nodes = _structural_nodes(document_id, body_id, laid_out["blocks"])
        nodes.extend(document_nodes)
        segments.extend(
            _search_segments(body_id, laid_out["blocks"], document_nodes, rendition_sha)
        )

    # ─── Attachments (amendment B4) ───────────────────────────────────
    #
    # Decision 0001 requires a row for every attachment the owner's source
    # record enumerates, and the pinned catalog's `candidateRenditions` are that
    # enumeration here. Two rows, three renditions, three of the four
    # dispositions:
    #
    #   FR-2026-03227  the html rendition IS the owning body's own rendition,
    #                  so it is enumerated and `text-excluded` rather than
    #                  extracted a second time (B4's resolution of the decision's
    #                  own ambiguity); its pdf sibling was never fetched. The row
    #                  carries no text and says so with a null `textBodyId`.
    #   FR-2026-04188  an appendix published as html and as pdf; the html was
    #                  captured and extracted, the pdf was not fetched. The row
    #                  IS a text body, with its own representation, structure,
    #                  and segments under the document body's rules unchanged.
    #
    # The first row's `text-excluded` capture names bytes that belong to ANOTHER
    # body's slice of a shared bucket, which is exactly the case amendment B4's
    # digest-addressed index lookup exists for. `extraction-failed` stays at
    # zero, as it does in the real mint: a rendition that was fetched and then
    # refused needs bytes in a bucket no text body owns, and the index has no key
    # for one.
    attachments: list[dict[str, Any]] = []
    documents_by_id = {document["documentId"]: document for document in documents}

    def _attachment_id(owner_body_id: str, identity: str) -> str:
        return stable_urn(
            "document-release-attachment",
            {
                "attachmentIdentity": identity,
                "ownerKind": DOCUMENT_BODY,
                "ownerTextBodyId": owner_body_id,
            },
            version=2,
        )

    def _unfetched(ordinal: int, media_type: str) -> dict[str, Any]:
        return {
            "attachmentDisposition": "source-unavailable",
            "capture": None,
            "mediaType": media_type,
            "reason": "The source enumerated this rendition and no copy of it was preserved.",
            "reasonCode": "no-preserved-copy",
            "renditionOrdinal": ordinal,
        }

    owner = documents_by_id["FR-2026-03227"]
    attachments.append(
        {
            "attachmentId": _attachment_id(owner[TEXT_BODY_KEY], "2026-03227.html"),
            "attachmentIdentity": "2026-03227.html",
            "attachmentTitle": None,
            "excludedRanges": [],
            "ownerKind": DOCUMENT_BODY,
            "ownerTextBodyId": owner[TEXT_BODY_KEY],
            "renditions": [
                {
                    "attachmentDisposition": "text-excluded",
                    "capture": dict(owner["capture"]),
                    "mediaType": "text/html",
                    "reason": (
                        "These bytes are the owning document body's own rendition; its text is "
                        "carried once, on the document row."
                    ),
                    "reasonCode": "owner-body-rendition",
                    "renditionOrdinal": 0,
                },
                _unfetched(1, "application/pdf"),
            ],
            "representation": None,
            TEXT_BODY_KEY: None,
            "textKind": "attachment",
        }
    )

    owner = documents_by_id["FR-2026-04188"]
    appendix_id = _attachment_id(owner[TEXT_BODY_KEY], "2026-04188-appendix")
    laid_out = _build_document_bytes("FR-2026-04188-appendix")
    rendition_key, rendition_sha = _place("blob", "blobs", appendix_id, laid_out["rendition"])
    representation_key, representation_sha = _place(
        "text", "text", appendix_id, laid_out["representation"]
    )
    retention_ratios.append(
        (
            normalized_byte_size(laid_out["representation"]),
            normalized_byte_size(laid_out["rendition"]),
        )
    )
    attachment_nodes = _structural_nodes(
        "FR-2026-04188-appendix", appendix_id, laid_out["blocks"], "attachment"
    )
    nodes.extend(attachment_nodes)
    segments.extend(
        _search_segments(
            appendix_id, laid_out["blocks"], attachment_nodes, rendition_sha, "attachment"
        )
    )
    attachments.append(
        {
            "attachmentId": appendix_id,
            "attachmentIdentity": "2026-04188-appendix",
            "attachmentTitle": None,
            "excludedRanges": [],
            "ownerKind": DOCUMENT_BODY,
            "ownerTextBodyId": owner[TEXT_BODY_KEY],
            "renditions": [
                {
                    "attachmentDisposition": "text-captured",
                    "capture": {
                        "acquiredAt": ACQUIRED_AT,
                        "acquisitionStartedAt": None,
                        "byteSize": len(laid_out["rendition"]),
                        "candidateRenditionId": "2026-04188-appendix.html",
                        "catalogReleaseId": catalog["catalogId"],
                        "expectedSha256": None,
                        "mediaType": "text/html",
                        "objectKey": rendition_key,
                        "sha256": rendition_sha,
                    },
                    "mediaType": "text/html",
                    "renditionOrdinal": 0,
                },
                _unfetched(1, "application/pdf"),
            ],
            "representation": {
                "byteSize": len(laid_out["representation"]),
                "encoding": "utf-8",
                "mediaType": REPRESENTATION_MEDIA_TYPE,
                "objectKey": representation_key,
                "representationId": f"{appendix_id}#representation",
                "sha256": representation_sha,
            },
            TEXT_BODY_KEY: appendix_id,
            "textKind": "attachment",
        }
    )

    # ─── The one comment (amendment C6) ───────────────────────────────
    #
    # A comment is a text body under the document body's rules unchanged, so it
    # is laid out, placed, structured, and segmented by the same functions the
    # bodies above are. What is its own is the row: `commentId` EQUALS its
    # `textBodyId`, its owner is exactly one document, and it projects the sealed
    # upstream selection policy verbatim -- the policy DocSpec inherits and never
    # computes. Under the fixture's pinned catalog no comment is a member of `U`,
    # which is not an obstacle: the universe bijection binds document bodies, and
    # a comment reaches a release as a text body of a document that is a member.
    comment_owner = documents_by_id[COMMENT_OWNER_DOCUMENT_ID]
    laid_out = _build_document_bytes("comment-" + COMMENT_ID)
    comment_blob_key, comment_blob_sha = _place(
        "blob", "blobs", COMMENT_ID, laid_out["rendition"]
    )
    comment_text_key, comment_text_sha = _place(
        "text", "text", COMMENT_ID, laid_out["representation"]
    )
    retention_ratios.append(
        (
            normalized_byte_size(laid_out["representation"]),
            normalized_byte_size(laid_out["rendition"]),
        )
    )
    comment_nodes = _structural_nodes(
        "comment-" + COMMENT_ID, COMMENT_ID, laid_out["blocks"], "comment"
    )
    nodes.extend(comment_nodes)
    segments.extend(
        _search_segments(
            COMMENT_ID, laid_out["blocks"], comment_nodes, comment_blob_sha, "comment"
        )
    )
    comments: list[dict[str, Any]] = [
        {
            "capture": {
                "acquiredAt": ACQUIRED_AT,
                "acquisitionStartedAt": None,
                "byteSize": len(laid_out["rendition"]),
                "candidateRenditionId": f"{COMMENT_ID}.html",
                "catalogReleaseId": catalog["catalogId"],
                "expectedSha256": None,
                "mediaType": POLICY_MEDIA_TYPE,
                "objectKey": comment_blob_key,
                "sha256": comment_blob_sha,
            },
            "commentId": COMMENT_ID,
            "commentSelection": {
                "groupBy": "/data/id",
                "orderBy": "/data/attributes/modifyDate DESC NULLS LAST",
                "policyDigest": COMMENT_SELECTION_POLICY_DIGEST,
                "selectedModifyDate": COMMENT_MODIFY_DATE,
                "tieDisposition": "refuse-repeated-normalized-instant",
            },
            "documentId": comment_owner["documentId"],
            "excludedRanges": [],
            "representation": {
                "byteSize": len(laid_out["representation"]),
                "encoding": "utf-8",
                "mediaType": REPRESENTATION_MEDIA_TYPE,
                "objectKey": comment_text_key,
                "representationId": f"{COMMENT_ID}#representation",
                "sha256": comment_text_sha,
            },
            "sourceItemId": comment_owner["sourceItemId"],
            "sourceIssuedVersion": comment_owner["sourceIssuedVersion"],
            TEXT_BODY_KEY: COMMENT_ID,
            "textKind": "comment",
        }
    ]

    for object_key, payload in sorted(partitions.items()):
        path = bundle / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(payload))

    # Amendment A4 lifts the multi-body refusal WHERE THE INDEX COVERS THE
    # BUCKET, and no further: a bucket holding several bodies with no slice for
    # one of them is still unrecoverable, and is still refused here rather than
    # minted and left for a consumer to discover.
    spans: dict[str, list[tuple[int, int]]] = {}
    for row in index_rows:
        spans.setdefault(row["member"], []).append((row["startByte"], row["byteLength"]))
    uncovered: list[str] = []
    for object_key, payload in sorted(partitions.items()):
        cursor = 0
        for start, length in sorted(spans.get(object_key, [])):
            if start != cursor:
                break
            cursor += length
        if cursor != len(payload):
            uncovered.append(object_key)
    if uncovered:
        raise SystemExit(
            "cannot mint this corpus: the text-body index does not tile partitions "
            + ", ".join(uncovered)
            + ", so one body's bytes cannot be recovered from the bucket it shares."
        )
    index_rows.sort(key=lambda row: (row["family"], row["textBodyId"]))

    state = {
        "attachments": attachments,
        "catalog": catalog,
        "comments": comments,
        "dispositions": dispositions,
        "documents": documents,
        "nodes": nodes,
        "processingPolicies": _processing_policies(
            min(_decimal_fraction(kept, total) for kept, total in retention_ratios)
        ),
        "segments": segments,
        "textBodyIndex": index_rows,
    }
    _restamp(bundle, state)
    return state


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _state(bundle: Path) -> dict[str, Any]:
    root = _load(bundle / "release.json")
    return {
        "attachments": load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl"),
        "catalog": root["content"]["sourceCatalog"],
        "comments": load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl"),
        "dispositions": load_strict_canonical_jsonl(bundle / "data" / "source-dispositions.jsonl"),
        "documents": load_strict_canonical_jsonl(bundle / "data" / "documents.jsonl"),
        "nodes": load_strict_canonical_jsonl(bundle / "data" / "structural-nodes.jsonl"),
        "processingPolicies": root["content"]["processingPolicies"],
        "segments": load_strict_canonical_jsonl(bundle / "data" / "search-segments.jsonl"),
        "textBodyIndex": load_strict_canonical_jsonl(bundle / TEXT_BODY_INDEX_KEY),
    }


def _diagnostics(bundle: Path) -> list[dict[str, Any]]:
    """Every diagnostic one bundle produces, as this build observes it.

    Regenerated from current behaviour rather than written down: a hand-written
    expectation in a corpus about diagnostics would test the author's memory.
    What makes it an expectation is that it is SEALED here and asserted
    thereafter, so the next change to a rule shows up as a diff in this list.
    """

    return [
        {"code": issue.code, "path": issue.path}
        for issue in verify_document_release(bundle).issues
    ]


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
            "expectedDiagnostics": _diagnostics(valid),
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
                # Amendment B3: the WHOLE diagnostic set, not just the primary
                # code and path. A bundle that emits its expected diagnostic
                # plus five others used to pass; now every code and path a case
                # produces is sealed, so a rule that starts firing where it did
                # not is a test failure rather than a silent widening.
                "expectedDiagnostics": _diagnostics(bundle),
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
            member["objectKey"] = "../escaped-search-segments.jsonl"
    manifest["members"].sort(key=lambda member: member["objectKey"])
    write_canonical_json(bundle / "manifests" / "global.json", manifest)
    root = _load(bundle / "release.json")
    root["content"]["globalManifest"]["byteSize"] = (bundle / "manifests" / "global.json").stat().st_size
    root["content"]["globalManifest"]["sha256"] = file_sha256(bundle / "manifests" / "global.json")
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record("unsafe-path", "invalid.path", "manifests/global.json/members/0/objectKey", bundle)

    bundle = copy_case("missing-member")
    # The partition member holding one document's representation, named from the
    # fixture's own rows rather than written down.
    missing_key = _state(bundle)["documents"][1]["representation"]["objectKey"]
    (bundle / missing_key).unlink()
    record("missing-member", "invalid.membership-missing", missing_key, bundle)

    bundle = copy_case("extra-member")
    (bundle / "undeclared.json").write_bytes(b"{}")
    record("extra-member", "invalid.membership-extra", "undeclared.json", bundle)

    bundle = copy_case("member-digest")
    (bundle / "data" / "structural-nodes.jsonl").write_bytes(
        (bundle / "data" / "structural-nodes.jsonl").read_bytes() + b" "
    )
    record("member-digest", "invalid.member-digest", "data/structural-nodes.jsonl", bundle)

    bundle = copy_case("unknown-node-kind")
    state = _state(bundle)
    state["nodes"][0]["nodeKind"] = "chapter"
    _restamp(bundle, state)
    record(
        "unknown-node-kind",
        "invalid.schema",
        "data/structural-nodes.jsonl/0/nodeKind",
        bundle,
    )

    bundle = copy_case("duplicate-segment")
    state = _state(bundle)
    state["segments"][1]["segmentId"] = state["segments"][0]["segmentId"]
    _restamp(bundle, state)
    record(
        "duplicate-segment",
        "invalid.duplicate-identity",
        "data/search-segments.jsonl/1/segmentId",
        bundle,
    )

    bundle = copy_case("catalog-pin-mismatch")
    root = _load(bundle / "release.json")
    # A well-formed catalog id that is not the one every capture row names, so
    # the case fails the pin rule rather than the id pattern.
    root["content"]["sourceCatalog"]["catalogId"] = (
        "urn:docspec:source-catalog:v1:" + "0" * 64
    )
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record(
        "catalog-pin-mismatch",
        "invalid.source-catalog-pin",
        "data/documents.jsonl/0/capture/catalogReleaseId",
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
        f"data/source-dispositions.jsonl/{index}/reason",
        bundle,
    )

    bundle = copy_case("expected-digest-mismatch")
    state = _state(bundle)
    state["documents"][0]["capture"]["expectedSha256"] = "sha256:" + "0" * 64
    _restamp(bundle, state)
    record(
        "expected-digest-mismatch",
        "invalid.capture",
        "data/documents.jsonl/0/capture/expectedSha256",
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
        "data/documents.jsonl/0/representation/sha256",
        bundle,
    )

    bundle = copy_case("orphan-structural-parent")
    state = _state(bundle)
    state["nodes"][2]["structuralParentId"] = f"{state['nodes'][2][TEXT_BODY_KEY]}#missing"
    _restamp(bundle, state)
    record(
        "orphan-structural-parent",
        "invalid.structure",
        "data/structural-nodes.jsonl/2/structuralParentId",
        bundle,
    )

    bundle = copy_case("segment-heading-path")
    state = _state(bundle)
    state["segments"][2]["headingPath"] = ["Wrong Heading"]
    _restamp(bundle, state)
    record(
        "segment-heading-path",
        "invalid.segment",
        "data/search-segments.jsonl/2/headingPath",
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
        "data/documents.jsonl/0/representation",
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

    bundle = copy_case("version-binding")
    state = _state(bundle)
    # The source-issued version moves on BOTH rows that carry it, so the join
    # still agrees and the only thing left broken is what the document version
    # id embeds: `documentId@sourceIssuedVersion` (amendment B2).
    moved = "2026-02-14T09:12:01Z"
    document = state["documents"][0]
    for row in state["dispositions"]:
        if row["documentId"] == document["documentId"]:
            row["sourceIssuedVersion"] = moved
    document["sourceIssuedVersion"] = moved
    _restamp(bundle, state)
    record(
        "version-binding",
        "invalid.version-binding",
        "data/documents.jsonl/0/documentVersionId",
        bundle,
    )

    # ─── The three diagnostics Decision 0001 named and amendment B4 landed ──
    bundle = copy_case("attachment-accounting")
    state = _state(bundle)
    # Sparse rendition ordinals. The schema admits any non-negative integer, so
    # this is a rule the accounting owns and nothing else can see.
    state["attachments"][0]["renditions"][1]["renditionOrdinal"] = 2
    _restamp(bundle, state)
    record(
        "attachment-accounting",
        "invalid.attachment-accounting",
        "data/attachments.jsonl/0/renditions",
        bundle,
    )

    bundle = copy_case("duplicate-attachment")
    state = _state(bundle)
    # Two rows, one identity. Under the `/3` domains the physical locators are
    # excluded, so a digest that deduped would let a multiplicity change pass
    # unnamed; the digester refuses instead, and the duplicate is reported for
    # the rule it breaks rather than as a set-digest mismatch.
    # The row is duplicated whole rather than renamed, so its derived id stays
    # correct and the case fails for multiplicity alone.
    state["attachments"].append(json.loads(json.dumps(state["attachments"][0])))
    _restamp(bundle, state)
    record(
        "duplicate-attachment",
        "invalid.duplicate-identity",
        "data/attachments.jsonl/2/attachmentId",
        bundle,
    )

    bundle = copy_case("retention-floor")
    state = _state(bundle)
    # A floor with no margin under the lowest legitimate document: the observed
    # minimum is dropped to the floor's own value, which is the shape the
    # superseded first mint's calibration would have had if its sample statistic
    # had been the population's.
    floor = state["processingPolicies"][0]["retentionFloor"]
    floor["observedMinimum"] = floor["value"]
    _restamp(bundle, state)
    record(
        "retention-floor",
        "invalid.retention-floor",
        "release.json/content/processingPolicies/0/retentionFloor/observedMinimum",
        bundle,
    )

    bundle = copy_case("comment-selection")
    state = _state(bundle)
    # Two policy digests in one release is the release claiming a selection
    # nobody sealed, and no schema can see it because a schema reads one row at a
    # time. This case could not be minted until amendment C6 gave the corpus a
    # comment; until then the rule was proved only on a grown bundle and the
    # absence was recorded rather than closed.
    #
    # The twin shares the first comment's bytes: what is under test is the policy
    # it projects, not the body it carries, and a second comment with no text of
    # its own would break the coverage identity instead.
    second = json.loads(json.dumps(state["comments"][0]))
    second["commentId"] = TWIN_COMMENT_ID
    second[TEXT_BODY_KEY] = TWIN_COMMENT_ID
    second["commentSelection"]["policyDigest"] = TWIN_SELECTION_POLICY_DIGEST
    state["comments"].append(second)
    for row in [*state["nodes"], *state["segments"]]:
        if row["textKind"] != "comment":
            continue
        twin = json.loads(json.dumps(row))
        twin[TEXT_BODY_KEY] = TWIN_COMMENT_ID
        for field in ("structuralNodeId", "segmentId", "structuralParentId"):
            if twin.get(field):
                twin[field] = twin[field].replace(row[TEXT_BODY_KEY], TWIN_COMMENT_ID)
        (state["nodes"] if "structuralNodeId" in twin else state["segments"]).append(twin)
    _restamp(bundle, state)
    record(
        "comment-selection",
        "invalid.comment-selection",
        "data/comments.jsonl/1/commentSelection/policyDigest",
        bundle,
    )

    # ─── The four rules amendment C1/C3/C4 turned from prose into checks ───
    bundle = copy_case("ungoverned-media-type")
    state = _state(bundle)
    # The document-body policy is re-declared for a media type no row carries.
    # Amendment B4's second `invalid.retention-floor` arm -- "a text body whose
    # `(textKind, mediaType)` has no governing policy" -- shipped with no fixture
    # at all, and amendment C1 found it broken: it collapsed both sides onto the
    # retention format key before comparing, which is exactly the blindness that
    # let a release declare `application/xml` over 6,408 `text/xml` rows.
    policy = next(
        item for item in state["processingPolicies"] if item["textKind"] == DOCUMENT_BODY
    )
    policy["mediaType"] = "text/xml"
    state["processingPolicies"] = sorted(
        state["processingPolicies"], key=lambda item: (item["textKind"], item["mediaType"])
    )
    _restamp(bundle, state)
    record(
        "ungoverned-media-type",
        "invalid.retention-floor",
        "data/documents.jsonl/0/capture/mediaType",
        bundle,
    )

    bundle = copy_case("selected-source-set-digest")
    root = _load(bundle / "release.json")
    # Amendment C3: the pin-derived attestation must agree with the members the
    # release's own disposition rows denote. Before C3 this value was checked for
    # FORM alone, so any well-formed digest passed.
    root["content"]["selectedSourceSetDigest"] = "sha256:" + "0" * 64
    write_canonical_json(bundle / "release.json", stamp_root(root))
    record(
        "selected-source-set-digest",
        "invalid.set-digest",
        "release.json/content/selectedSourceSetDigest",
        bundle,
    )

    bundle = copy_case("unknown-disposition-reason-code")
    state = _state(bundle)
    # Well-formed under the schema's dotted pattern and outside the closed list
    # amendment B7 wrote down and amendment C4 enforces, so the case fails for
    # the vocabulary rather than for the pattern.
    index = next(
        i
        for i, row in enumerate(state["dispositions"])
        if row["catalogDisposition"] == "unavailable"
    )
    state["dispositions"][index]["reasonCode"] = "capture.invented-code"
    _restamp(bundle, state)
    record(
        "unknown-disposition-reason-code",
        "invalid.disposition",
        f"data/source-dispositions.jsonl/{index}/reasonCode",
        bundle,
    )

    bundle = copy_case("unknown-attachment-reason-code")
    state = _state(bundle)
    # Kebab-case, so the schema admits it; outside the three-code attachment
    # list, so amendment C4 does not.
    state["attachments"][0]["renditions"][1]["reasonCode"] = "invented-code"
    _restamp(bundle, state)
    record(
        "unknown-attachment-reason-code",
        "invalid.attachment-accounting",
        "data/attachments.jsonl/0/renditions/1/reasonCode",
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
            "refusing to rebuild in place: re-minting a sealed corpus is a decision "
            "about the wire contract, not a mechanical refresh. Pass "
            "--allow-regeneration to re-mint deliberately. The predecessor corpus at "
            f"{PREDECESSOR_FIXTURE_ROOT.relative_to(REPO_ROOT).as_posix()} is frozen "
            "and is never written by this tool."
        )
        return 1

    cases = build_corpus()
    write_canonical_json(CORPUS_FILE, {"cases": cases})
    print(f"wrote {len(cases)} sealed cases to {CORPUS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
