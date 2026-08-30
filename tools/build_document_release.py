#!/usr/bin/env python3
"""Mint one sealed DocumentRelease 2.0 bundle from a pinned SourceCatalog.

`docs/decisions/0001-document-release-2-0.md` authorises this builder and fixes
what it does: pin one catalog (D1), adopt capture from preserved copies, extract
one visible-text representation per document under a declared retention floor,
bound the segments, and assemble a self-contained bundle whose identity is
derived from its own logical content. It implements the PRODUCE side only and
calls the moved verifier as its gate.

Reuse, not re-implementation
----------------------------
Every derived value comes from the code that already owns it:
`stamp_root`, `derive_counts`, `derive_coverage`, and `framed_set_digest` from
`adapters/document_release_verify.py`; the canonical byte, JSONL, and digest
primitives from `document_release_support.py`; `partition_bucket` from
`domain/storage.py`; the boundaries from `processing/bounded_segmentation.py`;
the floors from the committed calibration receipt. Nothing here re-derives a
digest algorithm, a set domain, or a segment boundary. It is the same debt the
fixture restamper pays, in the same currency.

Adopt and verify: preserved-copy is rung one
--------------------------------------------
The builder makes no request. Every rendition it carries is a preserved copy in
the pinned checkpoint's blob stores, re-digested against the acquisition record
that wrote it and, where the catalog declared one, against the catalog's own
expected digest. A blob that is absent, short, or differs is a **capture
failure** with a disposition and a reason -- never a reason to fetch. That is
the whole posture: the checkpoint is the source of bytes, and the release says
so or says why it could not.

The disposition mapping, recorded rather than implied
-----------------------------------------------------
Decision 0001 says the release projects "the catalog disposition ... verbatim".
The pinned snapshot has no such field to project. Its item vocabulary is
`state` -- `active`, `deleted`, `excluded` -- and `selected` is not in it,
because selection into a *release* is not something the catalog did. So the
mapping below is the builder's, and it is written down rather than assumed:

```text
deleted  -> deleted        the publisher withdrew it
excluded -> excluded       the catalog refused it, with the catalog's reason
active   -> selected       iff its bytes were adopted, extracted above the
                           floor, segmented, and its metadata was complete
         -> unavailable    no preserved copy, or one that failed verification
         -> failed         a parse or a segmentation this producer refused
         -> excluded       no markup rendition to select
```

The invariant Decision 0001 actually binds survives this intact and is checked
before the bundle is written: a `selected` row carries a `documentVersionId` and
a document row, any other disposition carries `null`, and no row carries a
processing failure. An item this producer could not make searchable is never
called `selected`; it is called what it was, with a machine-legible reason. Loss
stays visible instead of being accepted silently.

Usage:
  uv run python -m tools.build_document_release --output output/document-release-10k-v1
  uv run python -m tools.build_document_release --universe-sample 200 --output <dir>

`--universe-sample` is a development affordance, not a tier: it mints over a
deterministic stride through the pinned catalog so a test can exercise both
sources and both formats in seconds. The release it produces names itself in
`buildRunId` and is not the release D1 authorises, which has no sample.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docspec.adapters.document_release_verify import (
    DOCSPEC_GENERATION,
    FORMAT,
    FORMAT_VERSION,
    REPRESENTATION_MEDIA_TYPE,
    SCHEMA_FILES,
    SCHEMA_IDS,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
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
    canonical_sha256,
    file_sha256,
    write_canonical_json,
    write_canonical_jsonl,
)
from docspec.domain.storage import partition_bucket
from docspec.processing.bounded_segmentation import (
    BOUNDED_SEGMENTER_ID,
    BoundedSegmentationError,
    BoundedSegmenter,
    BoundedSegmentSettings,
    BoundedTextSegmentation,
)
from docspec.processing.retention_floors import (
    RetentionFloorError,
    RetentionFloorRegistry,
    format_key,
)
from docspec.processing.visible_text import (
    DEFAULT_VISIBLE_TEXT_EXTRACTORS,
    VisibleText,
    VisibleTextError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CORPUS_ID = "urn:docspec:document-corpus:us-federal-register"
DOCUMENT_BODY = "document-body"
TEXT_BODY_KEY = TEXT_BODY_KEYS[DOCSPEC_GENERATION]
TABULAR_MEDIA_TYPE = TABULAR_MEDIA_TYPES[DOCSPEC_GENERATION]
JOIN_RECEIPT_ID = "urn:docspec:join-receipt:source-to-document-v1"

# Decision 0001, restamp item 11: text and blob members follow the SourceCatalog
# multipart partition pattern, bucketing by digest of `textBodyId`.
PARTITION_BUCKET_COUNT = 64

# The declared byte ceiling one bounded segment may occupy. The segmenter bounds
# TOKENS, and `processingPolicies` must declare a byte bound a reader can check,
# so the bound is declared here and ENFORCED: a segmentation that produced a
# larger segment refuses its document rather than declaring a bound it broke.
# 64 KiB is roughly nine times the bytes an 1,800-token segment of this corpus
# occupies, which is margin for a document unlike any in it rather than a number
# chosen to be unreachable.
MAX_SEGMENT_BYTES = 65_536

# Refusal reason codes this builder owns, beside the extractor's and the floor's.
NO_MARKUP_RENDITION = "selection.no-markup-rendition"
NO_PRESERVED_COPY = "capture.no-preserved-copy"
UNVERIFIABLE_COPY = "capture.preserved-copy-unverifiable"
EXPECTED_DIGEST_DIFFERS = "capture.expected-digest-differs"
SEGMENTATION_REFUSED = "segmentation.refused"
SEGMENT_OVER_BOUND = "segmentation.segment-over-declared-bound"
NO_SEGMENT = "segmentation.no-searchable-segment"
INCOMPLETE_METADATA = "metadata.incomplete"

DELETED = "deleted"
EXCLUDED = "excluded"
UNAVAILABLE = "unavailable"
FAILED = "failed"
SELECTED = "selected"

# The catalog's item states, mapped onto the release's disposition vocabulary
# for the two that carry across unchanged. `active` is decided per item.
STATE_DISPOSITIONS: Mapping[str, str] = {"deleted": DELETED, "excluded": EXCLUDED}

MARKUP_MEDIA_TYPES = ("text/xml", "application/xml", "text/html")


class BuildRefusal(Exception):
    """One item this producer will not carry, and the disposition it earns."""

    def __init__(self, disposition: str, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.disposition = disposition
        self.reason_code = reason_code
        self.reason = reason


@dataclass
class _Partitions:
    """The digest-bucketed `text/` and `blobs/` members, written as they fill.

    Bodies are appended to their bucket's file rather than accumulated in
    memory: this corpus's captured bytes alone are 219 MB, and a builder that
    held them would be bounded by the corpus it can mint rather than by the
    corpus that exists.
    """

    root: Path
    handles: dict[str, Any] = field(default_factory=dict)
    lengths: dict[str, int] = field(default_factory=dict)
    index: list[dict[str, Any]] = field(default_factory=list)

    def place(self, family: str, object_key: str, body_id: str, payload: bytes) -> str:
        handle = self.handles.get(object_key)
        if handle is None:
            path = self.root / object_key
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("wb")
            self.handles[object_key] = handle
            self.lengths[object_key] = 0
        start = self.lengths[object_key]
        handle.write(payload)
        self.lengths[object_key] = start + len(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.index.append(
            {
                "byteLength": len(payload),
                "family": family,
                "member": object_key,
                "sha256": digest,
                "startByte": start,
                "textBodyId": body_id,
            }
        )
        return digest

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()


def _media_slug(media_type: str) -> str:
    """One partition-prefix segment naming a media type, safely and legibly."""

    return media_type.partition(";")[0].strip().casefold().replace("/", "-").replace("+", "-")


def _blob_key(media_type: str, body_id: str) -> str:
    """Bucket one captured rendition by text body, under its own media type.

    Digest bucketing mixes media types inside a bucket, and a `rendition` member
    declares exactly one `mediaType`. Scoping the buckets by media type keeps
    that declaration true without changing what decides the bucket.
    """

    return f"blobs/{_media_slug(media_type)}/{partition_bucket(body_id, PARTITION_BUCKET_COUNT):04d}"


def _text_key(body_id: str) -> str:
    return f"text/{partition_bucket(body_id, PARTITION_BUCKET_COUNT):04d}"


def _instant(value: Any) -> str | None:
    """Normalize one recorded wall clock to the whole seconds this contract carries.

    The acquisition records carry microseconds; `$defs/instant` admits whole
    seconds only. Truncated, never rounded: an acquisition is reported no later
    than it happened.
    """

    if not isinstance(value, str) or len(value) < 20:
        return None
    head = value[:19]
    return f"{head}Z" if head[10] == "T" else None


def _agencies(item: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[dict[str, str]]:
    """The publisher's own agency identifiers, carried rather than embellished.

    The Federal Register draw supplies slugs and the regulations.gov metadata
    supplies an agency code; neither supplies a display name, so `agencyName`
    repeats the identifier rather than inventing prose the source never wrote.
    """

    draw = metadata.get("finalDraw") or {}
    slugs = [slug for slug in str(draw.get("agency_slugs") or "").split(",") if slug]
    if slugs:
        return [{"agencyId": slug, "agencyName": slug} for slug in slugs]
    agency = (item.get("attributes") or {}).get("agencyId")
    return [{"agencyId": agency, "agencyName": agency}] if agency else []


def _source_metadata(
    item: Mapping[str, Any],
    candidate: Mapping[str, Any],
    attributes: Mapping[str, Any] | None,
    catalog_id: str,
) -> dict[str, Any]:
    """Project one catalog item's own metadata into the release's record.

    Open by construction: the release transports the producer's record rather
    than re-modelling it, so what the source supplied is carried and what it did
    not is absent rather than filled in.
    """

    metadata = item["metadata"]["qualification"]
    draw = metadata.get("finalDraw") or {}
    attributes = attributes or {}
    title = draw.get("title") or attributes.get("title")
    document_type = draw.get("document_type") or attributes.get("documentType")
    published = draw.get("publication_date") or attributes.get("postedDate")
    # The URL of the rendition this release actually carries, so a reader who
    # follows it sees the document rather than its metadata sibling.
    source_url = metadata.get("sourceUrl") or (candidate.get("metadata") or {}).get(
        "publicSourceUrl"
    )
    projected: dict[str, Any] = {
        "agencies": _agencies({"attributes": attributes}, metadata),
        "catalogReleaseId": catalog_id,
        "documentType": document_type,
        "publicationDate": published[:10] if isinstance(published, str) else None,
        "source": metadata.get("source"),
        "sourceUrl": source_url,
        "title": title,
    }
    if attributes.get("docketId"):
        projected["docketIds"] = [attributes["docketId"]]
    if draw.get("document_number"):
        projected["documentNumber"] = draw["document_number"]
    missing = [
        name
        for name in ("title", "documentType", "publicationDate", "sourceUrl")
        if not projected.get(name)
    ]
    if missing or not projected["agencies"]:
        raise BuildRefusal(
            FAILED,
            INCOMPLETE_METADATA,
            "the catalog item carries no " + ", ".join(missing or ["agency"]),
        )
    return projected


def _document_id(item: Mapping[str, Any]) -> str:
    metadata = item["metadata"]["qualification"]
    source = metadata["source"]
    if source == "federal-register":
        published = (metadata.get("finalDraw") or {}).get("document_number")
        return f"urn:docspec:document:v1:federal-register:{published}"
    return f"urn:docspec:document:v1:regulations-gov:{metadata['documentId']}"


def _markup_candidate(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """Choose the markup sibling, never the JSON one (Decision 0001).

    An item offering `text/html` or `text/xml` has that rendition as its body.
    An item whose only rendition is `application/json` has no declared floor and
    is refused rather than silently admitted.
    """

    for media_type in MARKUP_MEDIA_TYPES:
        for candidate in item.get("candidates", ()):
            if candidate.get("mediaType") == media_type:
                return candidate
    raise BuildRefusal(
        EXCLUDED,
        NO_MARKUP_RENDITION,
        "the catalog item offers no markup rendition, and a JSON-only item has no declared "
        "retention floor",
    )


def _adopt(candidate: Mapping[str, Any], preserved: Mapping[str, Any]) -> tuple[Any, bytes]:
    """Adopt one preserved copy, refusing anything that is not what was recorded."""

    capture = preserved.get(candidate["candidateId"])
    if capture is None:
        raise BuildRefusal(
            UNAVAILABLE,
            NO_PRESERVED_COPY,
            f"the checkpoint preserved no copy of candidate {candidate['candidateId']!r}",
        )
    if capture.media_type != candidate["mediaType"]:
        raise BuildRefusal(
            UNAVAILABLE,
            UNVERIFIABLE_COPY,
            f"the preserved copy declares {capture.media_type!r} where the catalog declares "
            f"{candidate['mediaType']!r}",
        )
    try:
        payload = capture.read()
    except Exception as error:  # the loader raises one type; the disposition is the same
        raise BuildRefusal(UNAVAILABLE, UNVERIFIABLE_COPY, str(error)) from error
    expected = candidate.get("expectedDigest")
    if expected is not None and expected != capture.digest:
        raise BuildRefusal(
            UNAVAILABLE,
            EXPECTED_DIGEST_DIFFERS,
            "the catalog's expected digest does not describe the preserved bytes",
        )
    return capture, payload


def _extract(payload: bytes, media_type: str, floors: RetentionFloorRegistry) -> tuple[VisibleText, str]:
    """Extract visible text and measure it against the floor that governs it."""

    key = format_key(media_type)
    extractor = DEFAULT_VISIBLE_TEXT_EXTRACTORS.get(key)
    if extractor is None:
        raise BuildRefusal(
            FAILED,
            "extraction.no-extractor",
            f"no visible-text extractor is registered for {key}",
        )
    try:
        visible = extractor.extract(payload)
    except VisibleTextError as error:
        raise BuildRefusal(FAILED, error.reason_code, error.reason) from error
    try:
        measured = floors.admit(
            DOCUMENT_BODY, media_type, retained=len(visible.content), source=len(payload)
        )
    except RetentionFloorError as error:
        raise BuildRefusal(FAILED, error.reason_code, error.reason) from error
    return visible, measured


def _structure(
    body_id: str, bounded: BoundedTextSegmentation, representation_size: int
) -> list[dict[str, Any]]:
    """Build the section tree the segmenter's own heading tiling implies.

    A root node spans the whole body, so text before the first heading has a
    parent; each heading opens a section that runs to the next heading at its
    own level or shallower. The nesting is the segmenter's -- the same push-and-
    pop `SegmentContext.headings` was built from -- so a segment's heading path
    and its structural ancestry cannot disagree.
    """

    root_id = f"{body_id}#n0"
    nodes = [
        {
            "depth": 0,
            "headingText": None,
            "nodeKind": "section",
            "ordinal": 0,
            "representationEnd": representation_size,
            "representationStart": 0,
            "structuralNodeId": root_id,
            "structuralParentId": None,
            TEXT_BODY_KEY: body_id,
            "textKind": DOCUMENT_BODY,
        }
    ]
    # (level, node index) for every open section, outermost first.
    stack: list[tuple[int, int]] = []
    siblings: dict[str | None, int] = {root_id: 0}
    for position, heading in enumerate(bounded.headings, start=1):
        while stack and stack[-1][0] >= heading.level:
            closed = stack.pop()
            nodes[closed[1]]["representationEnd"] = heading.start
        parent_index = stack[-1][1] if stack else 0
        parent_id = nodes[parent_index]["structuralNodeId"]
        ordinal = siblings.get(parent_id, 0)
        siblings[parent_id] = ordinal + 1
        node_id = f"{body_id}#n{position}"
        nodes.append(
            {
                "depth": nodes[parent_index]["depth"] + 1,
                "headingText": heading.title,
                "nodeKind": "heading",
                "ordinal": ordinal,
                "representationEnd": representation_size,
                "representationStart": heading.start,
                "structuralNodeId": node_id,
                "structuralParentId": parent_id,
                TEXT_BODY_KEY: body_id,
                "textKind": DOCUMENT_BODY,
            }
        )
        siblings.setdefault(node_id, 0)
        stack.append((heading.level, len(nodes) - 1))
    return nodes


def _heading_path(node_id: str, by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    chain: list[str] = []
    current: Mapping[str, Any] | None = by_id.get(node_id)
    while current is not None:
        if current["headingText"]:
            chain.append(current["headingText"])
        parent = current["structuralParentId"]
        current = by_id.get(parent) if parent is not None else None
    chain.reverse()
    return chain


def _segments(
    body_id: str,
    bounded: BoundedTextSegmentation,
    nodes: Sequence[Mapping[str, Any]],
    visible: VisibleText,
    rendition_sha256: str,
) -> list[dict[str, Any]]:
    """One row per bounded segment, parented to the section that contains it."""

    by_id = {node["structuralNodeId"]: node for node in nodes}
    ordered = sorted(
        (node for node in nodes if node["structuralParentId"] is not None),
        key=lambda node: node["representationStart"],
    )
    rows: list[dict[str, Any]] = []
    for ordinal, span in enumerate(bounded.spans):
        parent = nodes[0]
        for node in ordered:
            if node["representationStart"] <= span.start and span.end <= node["representationEnd"]:
                parent = node
        parent_id = parent["structuralNodeId"]
        path = _heading_path(parent_id, by_id)
        if tuple(path) != span.headings:
            raise BuildRefusal(
                FAILED,
                "structure.heading-path-disagrees",
                f"the section tree reports {path} where the segmenter reports {list(span.headings)}",
            )
        start, end = visible.rendition_range(span.start, span.end)
        rows.append(
            {
                "evidence": {
                    "coordinateSystem": "rendition-utf8-byte",
                    "end": end,
                    "renditionSha256": rendition_sha256,
                    "start": start,
                },
                "headingPath": path,
                "ordinal": ordinal,
                "representationEnd": span.end,
                "representationStart": span.start,
                "segmentId": f"{body_id}#s{ordinal}",
                "structuralParentId": parent_id,
                TEXT_BODY_KEY: body_id,
                "textKind": DOCUMENT_BODY,
            }
        )
    return rows


def _unique(rows: Iterable[Mapping[str, Any]], field_name: str) -> list[dict[str, Any]]:
    """One record per distinct key, first occurrence wins (the set the digest names)."""

    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(row[field_name], dict(row))
    return list(seen.values())


def _member(
    bundle: Path, object_key: str, *, role: str, record_count: int | None, schema_id: str, media_type: str
) -> dict[str, Any]:
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


DATA_MEMBERS: tuple[str, ...] = (
    "source-dispositions",
    "documents",
    "attachments",
    "comments",
    "structural-nodes",
    "search-segments",
)
TEXT_BODY_INDEX_KEY = "manifests/text-body-index.jsonl"


@dataclass(frozen=True, slots=True)
class BuildInputs:
    """Everything one mint reads, resolved and verified before it starts."""

    catalog_id: str
    catalog_digest: str
    items: Sequence[Mapping[str, Any]]
    captures: Mapping[str, Mapping[str, Any]]
    floors: RetentionFloorRegistry
    extractor_policies: Mapping[tuple[str, str], Mapping[str, str]]
    build_run_id: str
    published_at: str
    release_status: str = "candidate"


@dataclass
class BuildReport:
    """What the mint did, counted while it did it."""

    dispositions: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    retention: dict[str, list[str]] = field(default_factory=dict)
    max_segment_bytes: int = 0
    adopted_runs: dict[str, int] = field(default_factory=dict)

    def record(self, disposition: str, reason_code: str | None = None) -> None:
        self.dispositions[disposition] = self.dispositions.get(disposition, 0) + 1
        if reason_code is not None:
            self.refusals[reason_code] = self.refusals.get(reason_code, 0) + 1


def build_release(bundle: Path, inputs: BuildInputs) -> tuple[dict[str, Any], BuildReport]:
    """Mint one bundle into ``bundle`` and return its root and its report."""

    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "schemas").mkdir(parents=True, exist_ok=True)
    for path in SCHEMA_FILES.values():
        (bundle / "schemas" / path.name).write_bytes(path.read_bytes())

    counter = _token_counter()
    settings = BoundedSegmentSettings.for_counter(counter)
    segmenter = BoundedSegmenter(counter, settings=settings)

    partitions = _Partitions(bundle)
    report = BuildReport()
    dispositions: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    policies: dict[tuple[str, str], dict[str, Any]] = {}

    try:
        for item in inputs.items:
            document_id = _document_id(item)
            row: dict[str, Any] = {
                "catalogDisposition": SELECTED,
                "documentId": document_id,
                "documentVersionId": None,
                "processingFailures": [],
                "sourceIssuedVersion": item["version"],
                "sourceItemId": item["itemId"],
            }
            state = item.get("state")
            if state in STATE_DISPOSITIONS:
                row["catalogDisposition"] = STATE_DISPOSITIONS[state]
                row["reasonCode"] = f"catalog.state-{state}"
                row["reason"] = f"the pinned catalog records this item as {state}"
                report.record(row["catalogDisposition"], row["reasonCode"])
                dispositions.append(row)
                continue
            try:
                _carry(
                    item,
                    row,
                    document_id,
                    inputs,
                    segmenter,
                    partitions,
                    documents,
                    nodes,
                    segments,
                    policies,
                    report,
                )
            except BuildRefusal as refusal:
                row["catalogDisposition"] = refusal.disposition
                row["documentVersionId"] = None
                row["reasonCode"] = refusal.reason_code
                row["reason"] = refusal.reason
                report.record(refusal.disposition, refusal.reason_code)
            else:
                report.record(SELECTED)
            dispositions.append(row)
    finally:
        partitions.close()

    _require_bijection(dispositions, documents)
    index_rows = sorted(partitions.index, key=lambda entry: (entry["family"], entry["textBodyId"]))
    _require_tiling(index_rows, partitions.lengths)
    write_canonical_jsonl(bundle / TEXT_BODY_INDEX_KEY, index_rows)

    rows_by_role = {
        "source-dispositions": dispositions,
        "documents": documents,
        "attachments": [],
        "comments": [],
        "structural-nodes": nodes,
        "search-segments": segments,
    }
    members: list[dict[str, Any]] = []
    for role in DATA_MEMBERS:
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
    bucket_media = {
        document["capture"]["objectKey"]: document["capture"]["mediaType"] for document in documents
    }
    counts_by_member: dict[str, int] = {}
    for entry in index_rows:
        counts_by_member[entry["member"]] = counts_by_member.get(entry["member"], 0) + 1
    for object_key in sorted(counts_by_member):
        family_role = "representation" if object_key.startswith("text/") else "rendition"
        media_type = (
            REPRESENTATION_MEDIA_TYPE if family_role == "representation" else bucket_media[object_key]
        )
        members.append(
            _member(
                bundle,
                object_key,
                role=family_role,
                record_count=counts_by_member[object_key],
                schema_id=media_type,
                media_type=media_type,
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
    joined = _unique(
        (
            {
                "documentId": document["documentId"],
                "documentVersionId": document["documentVersionId"],
                "sourceItemId": document["sourceItemId"],
            }
            for document in documents
        ),
        "sourceItemId",
    )
    mapping = framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, joined)
    selected = [row for row in dispositions if row["catalogDisposition"] == SELECTED]
    content = {
        "attachmentSetDigest": framed_set_digest("docspec-attachment-set/2", ()),
        "commentSetDigest": framed_set_digest("docspec-comment-set/2", ()),
        "corpusId": CORPUS_ID,
        "counts": derive_counts(
            dispositions,
            documents,
            nodes,
            segments,
            member_count=len(members),
            total_member_byte_size=sum(member["byteSize"] for member in members),
            attachments=[],
            comments=[],
            generation=DOCSPEC_GENERATION,
        ),
        "coverage": derive_coverage(
            dispositions, documents, segments, key=TEXT_BODY_KEY, attachments=[], comments=[]
        ),
        "documentVersionSetDigest": framed_set_digest(
            "docspec-document-version-set/2",
            _unique(
                ({"documentVersionId": document["documentVersionId"]} for document in documents),
                "documentVersionId",
            ),
        ),
        "globalManifest": {
            "byteSize": (bundle / manifest_key).stat().st_size,
            "manifestId": "global:global",
            "objectKey": manifest_key,
            "scopeId": "global",
            "scopeKind": "global",
            "sha256": file_sha256(bundle / manifest_key),
        },
        "joinReceipt": {
            "documentVersionCount": len(documents),
            "mappingDigest": mapping,
            "receiptId": JOIN_RECEIPT_ID,
            "selectedSourceItemCount": len(selected),
        },
        "processingPolicies": sorted(
            policies.values(), key=lambda policy: (policy["textKind"], policy["mediaType"])
        ),
        "schemaSet": {
            "schemaSetId": f"urn:spicy:schema-set:v1:{canonical_sha256(schemas)}",
            "schemas": schemas,
        },
        "segmentSetDigest": framed_set_digest(
            "docspec-segment-set/2",
            _unique(({"segmentId": segment["segmentId"]} for segment in segments), "segmentId"),
        ),
        "selectedSourceSetDigest": framed_set_digest(SELECTED_SOURCE_SET_DOMAIN, joined),
        "sourceCatalog": {
            "catalogDigest": inputs.catalog_digest,
            "catalogId": inputs.catalog_id,
        },
        "sourceDocumentMappingDigest": mapping,
        "textBodySetDigest": framed_set_digest(
            "docspec-text-body-set/2",
            _unique(
                (
                    {"textBodyId": document[TEXT_BODY_KEY], "textKind": document["textKind"]}
                    for document in documents
                ),
                "textBodyId",
            ),
        ),
    }
    root = stamp_root(
        {
            "annotations": {
                "buildRunId": inputs.build_run_id,
                "publishedAt": inputs.published_at,
                "releaseStatus": inputs.release_status,
            },
            "content": content,
            "format": FORMAT,
            "formatVersion": FORMAT_VERSION,
        }
    )
    write_canonical_json(bundle / "release.json", root)
    return root, report


def _carry(
    item: Mapping[str, Any],
    row: dict[str, Any],
    document_id: str,
    inputs: BuildInputs,
    segmenter: BoundedSegmenter,
    partitions: _Partitions,
    documents: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, Any]],
    report: BuildReport,
) -> None:
    """Make one catalog item searchable, or refuse it with its disposition."""

    candidate = _markup_candidate(item)
    capture, payload = _adopt(candidate, inputs.captures.get(item["itemId"], {}))
    attributes = _attributes(item, inputs.captures.get(item["itemId"], {}))
    metadata = _source_metadata(item, candidate, attributes, inputs.catalog_id)

    media_type = candidate["mediaType"]
    visible, measured = _extract(payload, media_type, inputs.floors)
    try:
        bounded = segmenter.segment_text(visible.content, label=document_id)
    except BoundedSegmentationError as error:
        raise BuildRefusal(FAILED, SEGMENTATION_REFUSED, str(error)) from error
    if not bounded.spans:
        raise BuildRefusal(
            FAILED, NO_SEGMENT, "the visible text produced no searchable segment"
        )
    widest = max(span.end - span.start for span in bounded.spans)
    if widest > MAX_SEGMENT_BYTES:
        raise BuildRefusal(
            FAILED,
            SEGMENT_OVER_BOUND,
            f"a segment of {widest} bytes exceeds the declared bound of {MAX_SEGMENT_BYTES}",
        )
    report.max_segment_bytes = max(report.max_segment_bytes, widest)

    version_id = f"{document_id}@{item['version']}"
    body_id = version_id
    rendition_key = _blob_key(media_type, body_id)
    rendition_sha256 = partitions.place("blob", rendition_key, body_id, payload)
    representation_key = _text_key(body_id)
    representation_sha256 = partitions.place("text", representation_key, body_id, visible.content)

    document_nodes = _structure(body_id, bounded, len(visible.content))
    document_segments = _segments(body_id, bounded, document_nodes, visible, rendition_sha256)
    nodes.extend(document_nodes)
    segments.extend(document_segments)

    row["documentVersionId"] = version_id
    documents.append(
        {
            "capture": {
                "acquiredAt": _instant(capture.acquired_at),
                "acquisitionStartedAt": _instant(capture.acquisition_started_at),
                "byteSize": len(payload),
                "candidateRenditionId": candidate["candidateId"],
                "catalogReleaseId": inputs.catalog_id,
                "expectedSha256": candidate.get("expectedDigest"),
                "mediaType": media_type,
                "objectKey": rendition_key,
                "sha256": rendition_sha256,
            },
            "documentId": document_id,
            "documentVersionId": version_id,
            "excludedRanges": [item.to_dict() for item in bounded.excluded],
            "representation": {
                "byteSize": len(visible.content),
                "encoding": "utf-8",
                "mediaType": REPRESENTATION_MEDIA_TYPE,
                "objectKey": representation_key,
                "representationId": f"{body_id}#representation",
                "sha256": representation_sha256,
            },
            "sourceIssuedVersion": item["version"],
            "sourceItemId": item["itemId"],
            "sourceMetadata": metadata,
            TEXT_BODY_KEY: body_id,
            "textKind": DOCUMENT_BODY,
        }
    )
    report.adopted_runs[capture.run] = report.adopted_runs.get(capture.run, 0) + 1
    key = format_key(media_type)
    report.retention.setdefault(key, []).append(measured)
    policies.setdefault(
        (DOCUMENT_BODY, key),
        _policy(DOCUMENT_BODY, key, inputs, segmenter),
    )


def _attributes(
    item: Mapping[str, Any], preserved: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The regulations.gov metadata rendition's attributes, when one was preserved.

    The markup sibling is always the body (Decision 0001); the JSON sibling is
    where this publisher put the title, the agency, and the posted date, so it
    is read as metadata and never as text.
    """

    for candidate in item.get("candidates", ()):
        if candidate.get("mediaType") != "application/json":
            continue
        capture = preserved.get(candidate["candidateId"])
        if capture is None:
            continue
        try:
            document = json.loads(capture.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        data = document.get("data") if isinstance(document, Mapping) else None
        attributes = data.get("attributes") if isinstance(data, Mapping) else None
        return attributes if isinstance(attributes, Mapping) else None
    return None


def _policy(
    text_kind: str, key: str, inputs: BuildInputs, segmenter: BoundedSegmenter
) -> dict[str, Any]:
    """One `(textKind, mediaType)` policy, digesting the code that governed it."""

    declared = inputs.extractor_policies[(text_kind, key)]
    floor = inputs.floors.floor_for(text_kind, key)
    return {
        "extractorDigest": declared["extractorDigest"].removeprefix("sha256:"),
        "extractorId": declared["extractorId"],
        "maxSegmentBytes": MAX_SEGMENT_BYTES,
        "mediaType": key,
        "retentionFloor": floor.to_dict(),
        "segmenterDigest": segmenter.policy_digest.removeprefix("sha256:"),
        "segmenterId": BOUNDED_SEGMENTER_ID,
        "textKind": text_kind,
    }


def _require_bijection(
    dispositions: Sequence[Mapping[str, Any]], documents: Sequence[Mapping[str, Any]]
) -> None:
    """The one invariant Decision 0001 binds over the catalog universe.

    A `selected` row carries a `documentVersionId` and a document row; any other
    disposition carries `null`; no row carries a processing failure. A selected
    item that could not be made searchable would break the bijection, so it is
    never called selected -- and if one ever were, the build stops here rather
    than minting a release whose universe lies about itself.
    """

    versions = {document["documentVersionId"] for document in documents}
    problems: list[str] = []
    selected = 0
    for row in dispositions:
        if row["processingFailures"]:
            problems.append(f"{row['sourceItemId']} carries an accepted processing failure")
        if row["catalogDisposition"] == SELECTED:
            selected += 1
            if row["documentVersionId"] is None:
                problems.append(f"{row['sourceItemId']} is selected with no document version")
            elif row["documentVersionId"] not in versions:
                problems.append(f"{row['sourceItemId']} is selected with no document row")
        elif row["documentVersionId"] is not None:
            problems.append(f"{row['sourceItemId']} is {row['catalogDisposition']} with a version")
        if row["catalogDisposition"] != SELECTED and not row.get("reason"):
            problems.append(f"{row['sourceItemId']} is {row['catalogDisposition']} with no reason")
    if selected != len(documents):
        problems.append(f"{selected} selected rows against {len(documents)} document rows")
    if problems:
        raise SystemExit(
            "refusing to mint: the source-to-document join is not one-to-one\n  "
            + "\n  ".join(problems[:20])
        )


def _require_tiling(index_rows: Sequence[Mapping[str, Any]], lengths: Mapping[str, int]) -> None:
    """Amendment A4: every partitioned byte belongs to exactly one indexed slice."""

    spans: dict[str, list[tuple[int, int]]] = {}
    for row in index_rows:
        spans.setdefault(row["member"], []).append((row["startByte"], row["byteLength"]))
    uncovered = []
    for object_key, length in sorted(lengths.items()):
        cursor = 0
        for start, size in sorted(spans.get(object_key, [])):
            if start != cursor:
                break
            cursor += size
        if cursor != length:
            uncovered.append(object_key)
    if uncovered:
        raise SystemExit(
            "refusing to mint: the text-body index does not tile partitions "
            + ", ".join(uncovered)
            + ", so one body's bytes cannot be recovered from the bucket it shares."
        )


def _token_counter() -> Any:
    """The pinned tokenizer, or the deterministic fallback the settings admit.

    `BoundedSegmentSettings.for_counter` binds whichever one counted into the
    policy digest, and that digest rides in `processingPolicies`, so a release
    says which tokenizer measured its boundaries rather than leaving a reader to
    assume the pinned one.
    """

    try:
        from docspec.adapters.token_counters import TiktokenCounter

        return TiktokenCounter()
    except (ImportError, RuntimeError):
        return CodepointCounter()


class CodepointCounter:
    """One token per Unicode codepoint: exact, deterministic, no tokenizer.

    The fallback for a machine without the `tokens` extra. It is not a stand-in
    for the pinned tokenizer -- it counts a different thing and lands different
    boundaries -- which is exactly why its name and version ride in the
    segmenter policy digest a release carries.
    """

    name = "codepoints"
    version = "1"

    def count(self, text: str) -> int:
        return len(text)


# ─── The mint ──────────────────────────────────────────────────────────


MINT_RECEIPT_FORMAT = "docspec-document-release-mint-receipt"
MINT_RECEIPT_FORMAT_VERSION = "1.0"


def _quantiles(values: Sequence[str]) -> dict[str, str]:
    ordered = sorted(values, key=lambda value: value.ljust(12, "0"))
    return {
        "count": str(len(ordered)),
        "maximum": ordered[-1],
        "median": ordered[len(ordered) // 2],
        "minimum": ordered[0],
    }


def sample_universe(items: Sequence[Mapping[str, Any]], size: int) -> list[Mapping[str, Any]]:
    """A deterministic stride through the catalog, so a sample spans its sources."""

    if size >= len(items):
        return list(items)
    stride = len(items) // size
    return [items[index * stride] for index in range(size)]


def mint(
    output: Path,
    *,
    universe_sample: int | None = None,
    published_at: str | None = None,
    build_run_id: str | None = None,
) -> dict[str, Any]:
    """Run the whole first-mint pipeline and return its receipt.

    The receipt is the only thing this leaves in the repository. The bundle is
    data -- hundreds of megabytes of preserved bytes and the text extracted from
    them -- and lives under `output/`, which is not tracked.
    """

    from tools.calibrate_retention_floors import load_floors, load_policies
    from tools.fr_mirrulations_pin import catalog_items, load_pin, preserved_captures

    started = time.monotonic()
    pinned = load_pin()
    items = list(catalog_items(pinned))
    run_id = build_run_id or f"{pinned.campaign_id}-{pinned.tier}"
    if universe_sample is not None:
        items = sample_universe(items, universe_sample)
        run_id = f"{run_id}-sample-{len(items)}"
    captures = preserved_captures(pinned)
    inputs = BuildInputs(
        catalog_id=pinned.catalog_id,
        catalog_digest=pinned.catalog_digest,
        items=items,
        captures=captures,
        floors=RetentionFloorRegistry(load_floors()),
        extractor_policies=load_policies(),
        build_run_id=run_id,
        published_at=published_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    output = Path(output)
    root, report = build_release(output, inputs)
    built = time.monotonic()

    result = verify_document_release(output)
    verified = time.monotonic()
    content = root["content"]
    receipt = {
        "adoptedFromRun": dict(sorted(report.adopted_runs.items())),
        "bundle": {
            "memberCount": content["counts"]["memberCount"],
            "totalMemberByteSize": content["counts"]["totalMemberByteSize"],
        },
        "corpusPin": {
            "campaignId": pinned.campaign_id,
            "catalogDigest": f"sha256:{pinned.catalog_digest}",
            "catalogId": pinned.catalog_id,
            "drawDigests": {
                name: value["drawDigest"] for name, value in sorted(pinned.draw_digests.items())
            },
            "pinsId": pinned.pins_id,
            "tier": pinned.tier,
            "universeSample": len(items) if universe_sample is not None else None,
        },
        "counts": content["counts"],
        "coverage": content["coverage"],
        "dispositions": dict(sorted(report.dispositions.items())),
        "buildRunId": inputs.build_run_id,
        "documentStateDigest": root["documentStateDigest"],
        "format": MINT_RECEIPT_FORMAT,
        "formatVersion": MINT_RECEIPT_FORMAT_VERSION,
        "maxObservedSegmentBytes": report.max_segment_bytes,
        "processingPolicies": content["processingPolicies"],
        "refusals": dict(sorted(report.refusals.items())),
        "releaseId": root["releaseId"],
        "retention": {key: _quantiles(values) for key, values in sorted(report.retention.items())},
        "setDigests": {
            name: content[name]
            for name in sorted(
                (
                    "attachmentSetDigest",
                    "commentSetDigest",
                    "documentVersionSetDigest",
                    "segmentSetDigest",
                    "selectedSourceSetDigest",
                    "sourceDocumentMappingDigest",
                    "textBodySetDigest",
                )
            )
        },
        # Whole milliseconds, because the canonicaliser refuses a binary float
        # outright and a receipt that cannot be written canonically is a receipt
        # that cannot be sealed.
        "timingMilliseconds": {
            "build": int((built - started) * 1000),
            "total": int((verified - started) * 1000),
            "verify": int((verified - built) * 1000),
        },
        "verification": {
            "code": result.code,
            "diagnostics": [str(issue) for issue in result.issues[:20]],
            "diagnosticCount": len(result.issues),
            "generation": DOCSPEC_GENERATION,
        },
    }
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "output" / "document-release-10k-v1",
        help="where to materialize the bundle (data, not tracked)",
    )
    parser.add_argument(
        "--universe-sample",
        type=int,
        default=None,
        help="mint over a deterministic N-item stride through the catalog (development only)",
    )
    parser.add_argument("--published-at", default=None, help="the publication instant to stamp")
    parser.add_argument("--receipt", type=Path, default=None, help="where to write the mint receipt")
    arguments = parser.parse_args(argv)

    receipt = mint(
        arguments.output,
        universe_sample=arguments.universe_sample,
        published_at=arguments.published_at,
    )
    if arguments.receipt is not None:
        write_canonical_json(arguments.receipt, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["verification"]["code"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
