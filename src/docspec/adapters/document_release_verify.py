"""Portable verifier for sealed ``DocumentRelease`` v2 bundles.

Moved under REF-048 -- DocumentRelease is DocSpec's record -- from
`rulespec_conformance/document_release.py` at source commit
c584a1d9fcb89fb8c4253b5bb6879741b0e24c1c. Every rule, diagnostic code, and
ordering below is that file's; what changed is where the primitives, the
schemas, and the fixture corpus are read from, and the schema-identity
generation described under "Schema identity" below.

This module reads a materialized bundle of immutable files. It opens no
database, makes no network call, and imports no sibling product.

Why version 2.0
---------------
DocSpec's live root already writes ``format: "docspec-document-release"`` at
``formatVersion: "1.1"`` (`src/docspec/domain/release.py`), and that is a
different artifact: an internal pointer-record of active layers, blob roots,
and store receipts. This is the portable wire contract -- a self-contained
bundle of dispositions, captures, representations, structure, and segments.
Reusing the token at ``1.0`` would place the portable shape BELOW the internal
one on the same version line, so a reader would take 1.1 for a newer superset
of it. ``2.0``, with identity ``urn:docspec:document-release:v2:``, says what
is true: same product, same logical artifact, not compatible with 1.1.
`docs/decisions/0001-document-release-2-0.md` records the full deviation list.

Shared bundle protocol
----------------------
Canonical bytes, digests, tree digests, and path safety come from
`docspec.document_release_support`. They are imported rather than restated so
the traversal check has exactly one implementation, and so this module stays a
reader of DocSpec's byte rules rather than a second author of them.

Schema identity
---------------
A bundle names each schema it was written against by ``$id``. The 2.0 corpus
was sealed before REF-048 re-homed those identifiers, so its members carry the
predecessor generation while the packaged schemas carry ``urn:docspec:``. Both
generations name the same schema for the same role -- the bodies are identical
-- so `SCHEMA_ID_GENERATIONS` maps every accepted spelling onto the packaged
one, and every registry comparison runs on the resolved value. Nothing inside a
bundle is resolved away: an embedded schema's own ``$id`` must still equal the
descriptor that names it, and the schema-set digest is still taken over the
descriptors exactly as they were sealed. A bundle is read as it was written.

Minting generations
-------------------
Those two spellings are not only two names for one schema; they are two
*minting generations*, and identity and the set digests were minted differently
under each. `docs/decisions/0001-document-release-2-0.md` settles the
docspec-generation rules -- a ``documentStateDigest`` over the bundle's LOGICAL
content under the artifact canonicaliser, a ``releaseId`` DERIVED from it by
string form, and framed set digests under the ``/2`` domains that decision
declares. The twenty sealed bundles predate all of that and were minted under
the predecessor rules: a full-content digest under DocSpec's own canonicaliser
and plain sorted-set digests.

A verifier that applied either rule to both would be wrong about half its
corpus, so verification is generation-aware and keyed off the same declared
``$id``s `SCHEMA_ID_GENERATIONS` already resolves: `bundle_generation` reads the
generation the bundle itself declares, and a bundle is checked against the rules
it was minted under. Mixing the two spellings inside one bundle is refused
(`invalid.schema`) rather than resolved to a winner.

The generations differ in more than identity, because the restamp reshaped the
records themselves (Decision 0001, restamp items 4, 5, 11, 16): the docspec
generation carries tabular members as JSONL rather than as one JSON array, keys
structure and segments by ``textBodyId`` rather than by ``documentVersionId``,
and gives its partitioned ``text/`` and ``blobs/`` members a ``recordCount``
where the predecessor's one-file-per-document members declared null. Every one
of those rules is read off the same declared generation, so a bundle can never
be parsed under one and judged under the other.

Row schemas follow the same rule. The packaged schemas ARE the docspec
generation, so a docspec bundle's rows are checked against them AND its embedded
copies must equal them byte for byte -- the registered body stays the contract.
The predecessor generation's bodies are not packaged anywhere: they live in the
twenty sealed bundles that carry them, digest-pinned by their own descriptors and
sealed into their own identities, so a predecessor bundle's rows are checked
against the schemas it embeds. That is what carrying the schema set inside the
bundle was for.

This module lives beside the other adapters because it validates instances
against JSON Schema, and `jsonschema` is a third-party dependency the DocSpec
core deliberately does not import.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from rulespec_artifacts import FramedSection, framed_section_digest
from rulespec_artifacts import canonical_json_bytes as artifact_canonical_json_bytes

from docspec.domain.identity import stable_urn
from docspec.document_release_support import (
    MANIFEST_REFERENCE_FIELDS,
    MEMBER_DESCRIPTOR_FIELDS,
    MEMBER_MANIFEST_FORMAT,
    MEMBER_MANIFEST_VERSION,
    SUBORDINATE_MANIFEST_FIELDS,
    canonical_sha256,
    file_sha256,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
    logical_content,
    member_path,
    packaged_schema_root,
    safe_object_key,
    source_set_digest,
    tree_digest,
)

SCHEMA_ROOT = packaged_schema_root()
ROOT_SCHEMA = SCHEMA_ROOT / "document-release.schema.json"
MEMBER_MANIFEST_SCHEMA = SCHEMA_ROOT / "member-manifest.schema.json"
SOURCE_DISPOSITIONS_SCHEMA = SCHEMA_ROOT / "source-dispositions.schema.json"
DOCUMENTS_SCHEMA = SCHEMA_ROOT / "documents.schema.json"
STRUCTURAL_NODES_SCHEMA = SCHEMA_ROOT / "structural-nodes.schema.json"
SEARCH_SEGMENTS_SCHEMA = SCHEMA_ROOT / "search-segments.schema.json"
ATTACHMENTS_SCHEMA = SCHEMA_ROOT / "attachments.schema.json"
COMMENTS_SCHEMA = SCHEMA_ROOT / "comments.schema.json"

FORMAT = "docspec-document-release"
FORMAT_VERSION = "2.0"
RELEASE_ID_PREFIX = "urn:docspec:document-release:v2:"
SOURCE_CATALOG_ID_PREFIX = "urn:spicy-regs:source-catalog-release:v1:"

CATALOG_DISPOSITIONS: tuple[str, ...] = (
    "selected",
    "excluded",
    "deleted",
    "unavailable",
    "failed",
)
NON_SELECTED_DISPOSITIONS = frozenset(CATALOG_DISPOSITIONS) - {"selected"}

SCHEMA_FILES: dict[str, Path] = {
    "release-root": ROOT_SCHEMA,
    "member-manifest": MEMBER_MANIFEST_SCHEMA,
    "source-dispositions": SOURCE_DISPOSITIONS_SCHEMA,
    "documents": DOCUMENTS_SCHEMA,
    "attachments": ATTACHMENTS_SCHEMA,
    "comments": COMMENTS_SCHEMA,
    "structural-nodes": STRUCTURAL_NODES_SCHEMA,
    "search-segments": SEARCH_SEGMENTS_SCHEMA,
}

# The three text kinds, in the order `counts.perKind` declares them.
TEXT_KINDS: tuple[str, ...] = ("document-body", "attachment", "comment")
ATTACHMENT_DISPOSITIONS: tuple[str, ...] = (
    "text-captured",
    "text-excluded",
    "source-unavailable",
    "extraction-failed",
)
ATTACHMENT_URN_PREFIX = "urn:docspec:document-release-attachment:v2:"


def _registered_schema_id(path: Path) -> str:
    """Read one packaged schema's ``$id``, or say plainly that it is not there."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))["$id"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"packaged DocumentRelease schema is missing or unreadable: {path} ({exc})"
        ) from exc


SCHEMA_IDS: dict[str, str] = {
    role: _registered_schema_id(path) for role, path in SCHEMA_FILES.items()
}

# Every ``$id`` spelling a conforming 2.0 bundle may carry, mapped onto the
# packaged one. The predecessor generation is the identifier set the sealed
# corpus was minted under; it is frozen, so it is listed rather than derived. A
# spelling absent here is an unregistered schema and still fails closed.
_PREDECESSOR_SCHEMA_ID_BASE = "https://rulespec.org/schemas/releases"
_PREDECESSOR_SCHEMA_IDS: dict[str, str] = {
    "release-root": f"{_PREDECESSOR_SCHEMA_ID_BASE}/document-release-v2.schema.json",
    "member-manifest": f"{_PREDECESSOR_SCHEMA_ID_BASE}/document-release-v2/member-manifest-v1.schema.json",
    "source-dispositions": f"{_PREDECESSOR_SCHEMA_ID_BASE}/document-release-v2/source-dispositions-v1.schema.json",
    "documents": f"{_PREDECESSOR_SCHEMA_ID_BASE}/document-release-v2/documents-v1.schema.json",
    "structural-nodes": f"{_PREDECESSOR_SCHEMA_ID_BASE}/document-release-v2/structural-nodes-v1.schema.json",
    "search-segments": f"{_PREDECESSOR_SCHEMA_ID_BASE}/document-release-v2/search-segments-v1.schema.json",
}
SCHEMA_ID_GENERATIONS: dict[str, str] = {
    **{schema_id: schema_id for schema_id in SCHEMA_IDS.values()},
    **{
        predecessor: SCHEMA_IDS[role]
        for role, predecessor in _PREDECESSOR_SCHEMA_IDS.items()
        if role in SCHEMA_IDS
    },
}


def canonical_schema_id(value: Any) -> Any:
    """Resolve one declared schema ``$id`` onto the packaged spelling.

    An unregistered value is returned unchanged so the caller reports it as the
    mismatch it is, naming what the bundle actually declared.
    """

    return SCHEMA_ID_GENERATIONS.get(value, value)


# ─── Minting generations ───────────────────────────────────────────────

PREDECESSOR_GENERATION = "predecessor"
DOCSPEC_GENERATION = "docspec"

# The same two id sets `SCHEMA_ID_GENERATIONS` maps, read for the other fact
# they carry: which minting rules the bundle declaring them was written under.
# One table, two questions, so a bundle can never resolve its schemas under one
# generation and its identity under the other.
_GENERATION_OF_SCHEMA_ID: dict[str, str] = {
    **{schema_id: DOCSPEC_GENERATION for schema_id in SCHEMA_IDS.values()},
    **{schema_id: PREDECESSOR_GENERATION for schema_id in _PREDECESSOR_SCHEMA_IDS.values()},
}

# How many schemas a conforming bundle of each generation declares, and which
# roles. The docspec generation is the packaged eight (restamp item 3's 6 -> 8
# widening); the predecessor generation is the frozen six the sealed corpus was
# minted with, and `attachments`/`comments` are absent there because they did
# not exist. One table, so neither branch can silently demand the other's roles.
GENERATION_SCHEMA_ROLES: dict[str, frozenset[str]] = {
    PREDECESSOR_GENERATION: frozenset(_PREDECESSOR_SCHEMA_IDS),
    DOCSPEC_GENERATION: frozenset(SCHEMA_FILES),
}


def schema_id_generation(value: Any) -> str | None:
    """Name the minting generation one declared ``$id`` belongs to, or nothing."""

    return _GENERATION_OF_SCHEMA_ID.get(value)


def declared_generations(root: Mapping[str, Any]) -> set[str]:
    """Every registered generation this root's schema set declares.

    Unregistered spellings are dropped here rather than guessed at: they are
    already reported as unregistered schemas, and one unknown id must not
    silently move a bundle onto the other generation's identity rule.
    """

    content = root.get("content")
    schema_set = content.get("schemaSet") if isinstance(content, Mapping) else None
    descriptors = schema_set.get("schemas") if isinstance(schema_set, Mapping) else None
    if not isinstance(descriptors, list):
        return set()
    found = {
        schema_id_generation(descriptor.get("schemaId"))
        for descriptor in descriptors
        if isinstance(descriptor, Mapping)
    }
    found.discard(None)
    return {generation for generation in found if generation is not None}


def bundle_generation(root: Mapping[str, Any]) -> str:
    """Which generation's minting rules this bundle must be verified under.

    Only a root whose declared schema identifiers are ALL the docspec spelling
    is read under the docspec rules. Everything else -- the predecessor corpus,
    a mixed set, a root with no legible schema set at all -- is read under the
    predecessor rules, which is where a bundle that cannot say what it is
    belongs: they are the rules the only sealed bundles in existence were minted
    under, and a mixed or illegible set is separately refused as
    ``invalid.schema``.
    """

    return (
        DOCSPEC_GENERATION
        if declared_generations(root) == {DOCSPEC_GENERATION}
        else PREDECESSOR_GENERATION
    )


# Member roles that carry schema-governed rows, and the schema role serving each.
# `attachments` and `comments` were fail-closed here until restamp item 2 was
# resolvable: a role whose rows no sealed schema governs cannot be judged, and a
# role the verifier cannot judge must not pass unread. Both schemas are sealed
# now, so both roles are judged rather than refused -- under the docspec
# generation only. Under the predecessor generation they are still refused,
# because the schemas that would govern them are not that generation's.
TABULAR_ROLES: dict[str, str] = {
    "source-dispositions": "source-dispositions",
    "documents": "documents",
    "attachments": "attachments",
    "comments": "comments",
    "structural-nodes": "structural-nodes",
    "search-segments": "search-segments",
}
# The tabular members every generation carries. `attachments` and `comments` are
# the two the docspec generation added; the four here are the ones a predecessor
# bundle also declares, and the ones every bundle must declare exactly one of.
PREDECESSOR_TABULAR_ROLES: tuple[str, ...] = (
    "source-dispositions",
    "documents",
    "structural-nodes",
    "search-segments",
)
# The index over partitioned member bytes (amendment A4). Its rows are governed
# by the member-manifest schema's own `textBodyIndexRow` `$def` rather than by a
# ninth schema: an index over member bytes is a fact about how members are
# packed, which is the manifest's business, and restamp item 3 fixes the schema
# set at exactly eight.
TEXT_BODY_INDEX_ROLE = "text-body-index"
TEXT_BODY_INDEX_ROW_DEF = "textBodyIndexRow"
TEXT_BODY_INDEX_FAMILIES: dict[str, str] = {"text": "representation", "blob": "capture"}
# `rendition` and `representation`. Opaque in the sense that no row schema
# governs their bytes -- but under the docspec generation they are partition
# BUCKETS carrying a `recordCount`, not single documents (restamp items 11, 16).
OPAQUE_ROLES = frozenset({"rendition", "representation"})
ALLOWED_MEMBER_ROLES = frozenset(
    {"schema", TEXT_BODY_INDEX_ROLE, *TABULAR_ROLES, *OPAQUE_ROLES}
)
# One role vocabulary per generation, read off the same declaration everything
# else is. The predecessor corpus has no attachment, comment, or index member,
# and a bundle that declared one would be declaring a member this verifier could
# only judge against another generation's schemas.
MEMBER_ROLES_BY_GENERATION: dict[str, frozenset[str]] = {
    PREDECESSOR_GENERATION: frozenset(
        {"schema", *PREDECESSOR_TABULAR_ROLES, *OPAQUE_ROLES}
    ),
    DOCSPEC_GENERATION: ALLOWED_MEMBER_ROLES,
}
REPRESENTATION_MEDIA_TYPE = "text/plain; charset=utf-8"

# One fact per generation, read off the same declared `$id`s.
TABULAR_MEDIA_TYPES: dict[str, str] = {
    PREDECESSOR_GENERATION: "application/json",
    DOCSPEC_GENERATION: "application/x-ndjson",
}
# The field structure and segments hang off. The docspec generation re-keys to
# `textBodyId` so one set of records serves all three text kinds; for a document
# body the two values are equal, but the DECLARED key is the one that is read.
TEXT_BODY_KEYS: dict[str, str] = {
    PREDECESSOR_GENERATION: "documentVersionId",
    DOCSPEC_GENERATION: "textBodyId",
}

DIAGNOSTIC_CODES: tuple[str, ...] = (
    # Bundle integrity: nothing below can be judged until the bytes are trusted.
    "invalid.root-syntax",
    "invalid.format",
    "invalid.identity",
    "invalid.path",
    "invalid.membership-missing",
    "invalid.membership-extra",
    "invalid.member-digest",
    "invalid.schema",
    "invalid.duplicate-identity",
    # Domain, in dependency order: you cannot judge a segment before the
    # structure it hangs off, nor structure before the representation it
    # indexes, nor a representation before the capture it was extracted from.
    "invalid.source-catalog-pin",
    "invalid.disposition",
    "invalid.capture",
    "invalid.representation",
    "invalid.structure",
    "invalid.segment",
    "invalid.coverage",
    "invalid.join",
    "invalid.set-digest",
    "invalid.counts",
)
CODE_PRECEDENCE: dict[str, int] = {
    code: index for index, code in enumerate(DIAGNOSTIC_CODES)
}


@dataclass(frozen=True)
class VerificationIssue:
    """One deterministic conformance diagnostic."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.path}: {self.message}"


@dataclass(frozen=True)
class VerificationResult:
    """The ordered result of verifying one materialized bundle."""

    release_id: str | None
    issues: tuple[VerificationIssue, ...]

    @property
    def first(self) -> VerificationIssue | None:
        if not self.issues:
            return None
        return min(
            self.issues, key=lambda issue: CODE_PRECEDENCE.get(issue.code, 10_000)
        )

    @property
    def code(self) -> str:
        first = self.first
        return "valid" if first is None else first.code

    @property
    def path(self) -> str | None:
        first = self.first
        return None if first is None else first.path

    @property
    def valid(self) -> bool:
        return not self.issues


# ─── Identity and derived values ───────────────────────────────────────


def artifact_sha256(value: Any) -> str:
    """Digest one value under the artifact canonicaliser, unqualified.

    The docspec generation mints with `rulespec_artifacts.canonical_json_bytes`
    -- the container's canonicaliser, not DocSpec's -- so the container is the
    single minter of the top-level release name (Decision 0001, D2). The two
    encoders agree byte for byte on every value this format carries; they part
    company on their refusal surfaces and on object keys outside the Basic
    Multilingual Plane, which this format has none of. Which one signed a digest
    is therefore not observable from the digest, and that is exactly why the
    generation has to be read from the bundle rather than guessed at.
    """

    return hashlib.sha256(artifact_canonical_json_bytes(value)).hexdigest()


def expected_document_state_digest(root: Mapping[str, Any]) -> str:
    """The docspec-generation digest over this bundle's LOGICAL content.

    `logical_content` drops the physical and packing facts, so a repack that
    changes only how the bundle was written -- its member manifest, its member
    count, its total member byte size -- leaves this digest where it was. That
    is the INCREMENTAL-EQUIVALENCE property, and a flat hash over the whole
    root would break it.
    """

    payload = {
        "format": root.get("format"),
        "formatVersion": root.get("formatVersion"),
        "logicalContent": logical_content(root.get("content")),
    }
    return "sha256:" + artifact_sha256(payload)


def expected_release_id(root: Mapping[str, Any], *, generation: str | None = None) -> str:
    """Derive the release identity from the exact identity-bearing payload.

    ``annotations`` is excluded, and that is where every fact about the act of
    publishing lives. Unlike DocSpec's live root, the format token and version
    are INSIDE the preimage, so a future reshape cannot mint a colliding name.

    Under the docspec generation the name is not minted a second time: it is
    the URN prefix plus the ``documentStateDigest`` hex, by string form
    (Decision 0001, identity rule 2). Under the predecessor generation -- the
    twenty sealed bundles -- it is the full-content digest those bundles were
    sealed with, taken under DocSpec's own canonicaliser.
    """

    if (generation or bundle_generation(root)) == DOCSPEC_GENERATION:
        return RELEASE_ID_PREFIX + expected_document_state_digest(root).split(":", 1)[1]
    payload = {
        "format": root.get("format"),
        "formatVersion": root.get("formatVersion"),
        "content": root.get("content"),
    }
    return RELEASE_ID_PREFIX + canonical_sha256(payload)


def stamp_root(root: Mapping[str, Any]) -> dict[str, Any]:
    """Return a root copy carrying its content-derived identity.

    A docspec-generation root is stamped with both names, in the order the
    decision derives them: the state digest over logical content first, the
    release id from its hex second.
    """

    stamped = json.loads(json.dumps(root))
    stamped.pop("releaseId", None)
    generation = bundle_generation(stamped)
    if generation == DOCSPEC_GENERATION:
        stamped.pop("documentStateDigest", None)
        stamped["documentStateDigest"] = expected_document_state_digest(stamped)
    stamped["releaseId"] = expected_release_id(stamped, generation=generation)
    return stamped


def mapping_digest(pairs: Sequence[Sequence[str]]) -> str:
    """The PREDECESSOR generation's source-item/document-version pair digest.

    A LIST digest, not a set digest: under the rules the sealed corpus was
    minted with, the pairing IS the fact this release exists to carry, so a
    repeated pair moves the digest rather than being silently folded away.
    Duplication is separately reported by the join receipt and by
    `invalid.duplicate-identity`.

    The docspec generation does not use this. There the same fact is a framed
    SET digest over unique ``sourceItemId`` keys under
    ``docspec-source-to-document/2`` -- see `FRAMED_SET_DOMAINS`.
    """

    return "sha256:" + canonical_sha256(sorted([list(pair) for pair in pairs]))


# ─── Framed set digests: the docspec generation's ``/2`` domains ───────

# Decision 0001, "Sealed identities": 2.0's projections re-key from
# `documentVersionId` to `textBodyId`, so 2.0 declares its own domains at `/2`
# rather than reusing the spec's `/1` ones. Each maps a domain onto the exact
# record it streams; the FIRST field is the key, rows are ordered by the whole
# tuple under the shared UTF-16 rule, and a repeated key is refused. Every one
# of these is a SET digest over unique keys.
SELECTED_SOURCE_SET_DOMAIN = "docspec-selected-source-set/1"
SOURCE_TO_DOCUMENT_DOMAIN = "docspec-source-to-document/2"
FRAMED_SET_DOMAINS: dict[str, tuple[str, ...]] = {
    # The catalog's own domain, at `/1`, because the digest is the same fact
    # under the same name: the release DERIVES it over the pinned catalog's
    # items rather than projecting one the D1 snapshot does not carry, which
    # is why the name may stay (spec section 7.5, Decision 0001 restamp item 9).
    SELECTED_SOURCE_SET_DOMAIN: ("sourceItemId", "documentId"),
    "docspec-document-set/2": ("documentId",),
    "docspec-document-version-set/2": ("documentVersionId",),
    "docspec-text-body-set/2": ("textBodyId", "textKind"),
    "docspec-attachment-set/2": ("attachmentId",),
    "docspec-comment-set/2": ("commentId",),
    "docspec-representation-set/2": ("representationId",),
    "docspec-segment-set/2": ("segmentId",),
    SOURCE_TO_DOCUMENT_DOMAIN: ("sourceItemId", "documentId", "documentVersionId"),
}


def _utf16_key(value: str) -> bytes:
    """Order row keys by the shared artifact rule, not by Python's default.

    UTF-16 code units, the ordering `rulespec_artifacts` sorts object keys
    under, so a release digest and a catalog digest cannot disagree about what
    "sorted" means.
    """

    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as error:
        raise ValueError("set member identity contains a lone Unicode surrogate") from error


def framed_set_digest(domain: str, rows: Iterable[Mapping[str, Any]]) -> str:
    """Digest one bounded, UTF-16-ordered member stream under a 2.0 set domain.

    The framing itself is `rulespec_artifacts.framed_section_digest` -- the one
    implementation in the installed container, the same one
    `adapters/source_catalog_artifact.py:347-352` wraps -- so nothing here
    re-derives a digest algorithm. What is written out is the discipline around
    it that `selected_source_set_digest`
    (`adapters/source_catalog_artifact.py:375-395`) states: a declared count, a
    UTF-16-ordered key, and a refusal on a repeated key.
    """

    fields = FRAMED_SET_DOMAINS.get(domain)
    if fields is None:
        raise ValueError(f"{domain!r} is not a declared DocumentRelease 2.0 set domain")
    key, *_rest = fields
    records: list[dict[str, str]] = []
    for row in rows:
        record: dict[str, str] = {}
        for field in fields:
            value = row.get(field)
            if not isinstance(value, str):
                raise ValueError(f"{domain} member field {field!r} must be text")
            record[field] = value
        records.append(record)
    records.sort(key=lambda record: tuple(_utf16_key(record[field]) for field in fields))

    def stream() -> Iterable[Mapping[str, str]]:
        previous: bytes | None = None
        for record in records:
            current = _utf16_key(record[key])
            if previous is not None and current <= previous:
                raise ValueError(f"{domain} members must be sorted and distinct")
            previous = current
            yield record

    try:
        return framed_section_digest(domain, (FramedSection("members", len(records), stream()),))
    except (TypeError, ValueError) as error:
        raise ValueError(f"cannot compute {domain}: {error}") from error


def _interval_union(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge half-open intervals into a sorted, disjoint cover."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered_bytes(ranges: Sequence[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _interval_union(ranges))


def _text_bodies(
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> list[tuple[Any, str, Any, Any]]:
    """Every text body this release carries, as ``(id, kind, representation, excluded)``.

    One text pipeline, three kinds: a document body, an attachment, and a
    comment each have captured bytes, one selected representation, and an
    exclusion ledger, so the byte accounting reads them through one projection
    rather than three. An attachment with a null ``textBodyId`` carries no text
    -- every rendition of it failed or was excluded -- and is not a text body;
    it is still enumerated, still accounted, and still counted nowhere here.
    """

    bodies: list[tuple[Any, str, Any, Any]] = [
        (
            document.get(key),
            document.get("textKind") or "document-body",
            document.get("representation"),
            document.get("excludedRanges"),
        )
        for document in documents
    ]
    for attachment in attachments:
        if isinstance(attachment.get("textBodyId"), str):
            bodies.append(
                (
                    attachment["textBodyId"],
                    "attachment",
                    attachment.get("representation"),
                    attachment.get("excludedRanges"),
                )
            )
    for comment in comments:
        bodies.append(
            (
                comment.get("textBodyId"),
                "comment",
                comment.get("representation"),
                comment.get("excludedRanges"),
            )
        )
    return bodies


def _body_totals(
    body: tuple[Any, str, Any, Any],
    segments: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> tuple[int, int, int]:
    """One text body's representation, segmented, and excluded byte totals."""

    body_id, _kind, representation, excluded = body
    representation_bytes = (
        representation["byteSize"]
        if isinstance(representation, Mapping) and isinstance(representation.get("byteSize"), int)
        else 0
    )
    segmented = _covered_bytes(
        [
            (segment["representationStart"], segment["representationEnd"])
            for segment in segments
            if segment.get(key) == body_id
            and isinstance(segment.get("representationStart"), int)
            and isinstance(segment.get("representationEnd"), int)
        ]
    )
    excluded_bytes = _covered_bytes(
        [
            (item["start"], item["end"])
            for item in excluded or []
            if isinstance(item, Mapping)
            and isinstance(item.get("start"), int)
            and isinstance(item.get("end"), int)
        ]
    )
    return representation_bytes, segmented, excluded_bytes


def derive_per_kind_counts(
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> dict[str, dict[str, int]]:
    """Recompute ``counts.perKind`` from the members alone (amendment A2).

    Closed at the three text kinds and keyed by them, each carrying exactly
    ``textBodies``, ``segments``, and the three byte totals. Every kind is
    present even when the release carries none of it: a zero is written, never
    omitted, so a consumer reads absence as a measured zero rather than as a
    field somebody forgot.
    """

    per_kind = {
        kind: {
            "textBodies": 0,
            "segments": 0,
            "representationByteTotal": 0,
            "segmentedByteTotal": 0,
            "excludedByteTotal": 0,
        }
        for kind in TEXT_KINDS
    }
    for segment in segments:
        kind = segment.get("textKind") or "document-body"
        if kind in per_kind:
            per_kind[kind]["segments"] += 1
    for body in _text_bodies(documents, attachments, comments, key=key):
        kind = body[1]
        if kind not in per_kind:
            continue
        representation, segmented, excluded = _body_totals(body, segments, key=key)
        tally = per_kind[kind]
        tally["textBodies"] += 1
        tally["representationByteTotal"] += representation
        tally["segmentedByteTotal"] += segmented
        tally["excludedByteTotal"] += excluded
    return per_kind


def derive_counts(
    dispositions: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    *,
    member_count: int,
    total_member_byte_size: int,
    attachments: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
    generation: str = PREDECESSOR_GENERATION,
) -> dict[str, Any]:
    """Recompute the diagnostic counts from the members alone.

    ``perKind`` is the docspec generation's field and only that generation's:
    the sealed corpus was minted before amendment A2 named those fields, and a
    recomputation that added them would rename all twenty bundles.
    """

    tally = {name: 0 for name in CATALOG_DISPOSITIONS}
    for row in dispositions:
        value = row.get("catalogDisposition")
        if value in tally:
            tally[value] += 1
    counts: dict[str, Any] = {
        "requestedUniverseCount": len(dispositions),
        "selectedCount": tally["selected"],
        "excludedCount": tally["excluded"],
        "deletedCount": tally["deleted"],
        "unavailableCount": tally["unavailable"],
        "failedCount": tally["failed"],
        "documentVersionCount": len(documents),
        "structuralNodeCount": len(nodes),
        "searchSegmentCount": len(segments),
        "memberCount": member_count,
        "totalMemberByteSize": total_member_byte_size,
    }
    if generation == DOCSPEC_GENERATION:
        counts["perKind"] = derive_per_kind_counts(
            documents, attachments, comments, segments, key=TEXT_BODY_KEYS[generation]
        )
    return counts


def derive_coverage(
    dispositions: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    *,
    key: str = "documentVersionId",
    attachments: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
) -> dict[str, int]:
    """Recompute the accounting proof from the members alone.

    ``key`` is the field segments hang off in the generation being read --
    ``documentVersionId`` for the sealed corpus, ``textBodyId`` for the docspec
    generation. It defaults to the predecessor's so the sealed corpus keeps
    being derived exactly as it was sealed.

    ``coverage`` stays AGGREGATE (amendment A2): the byte totals span every text
    kind together, and the per-kind breakdown lives under ``counts.perKind``.
    ``documentsWithSegmentCount`` stays a fact about documents, because that is
    what it counts.
    """

    accounted = sum(
        1 for row in dispositions if row.get("catalogDisposition") in CATALOG_DISPOSITIONS
    )
    with_segment = {
        segment.get(key) for segment in segments if isinstance(segment.get(key), str)
    }
    representation_total = 0
    segmented_total = 0
    excluded_total = 0
    for body in _text_bodies(documents, attachments, comments, key=key):
        representation, segmented, excluded = _body_totals(body, segments, key=key)
        representation_total += representation
        segmented_total += segmented
        excluded_total += excluded
    return {
        "accountedCount": accounted,
        "unaccountedCount": len(dispositions) - accounted,
        "documentsWithSegmentCount": sum(
            1 for document in documents if document.get(key) in with_segment
        ),
        "representationByteTotal": representation_total,
        "segmentedByteTotal": segmented_total,
        "excludedByteTotal": excluded_total,
    }


# ─── Verification ──────────────────────────────────────────────────────


def _issue(issues: list[VerificationIssue], code: str, path: str, message: str) -> None:
    issues.append(VerificationIssue(code=code, path=path, message=message))


def _load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_issues(value: Any, schema: Mapping[str, Any], *, path: str) -> list[VerificationIssue]:
    validator = jsonschema.Draft202012Validator(schema)
    issues: list[VerificationIssue] = []
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path)):
        suffix = "".join(f"/{part}" for part in error.path)
        _issue(issues, "invalid.schema", f"{path}{suffix}", error.message)
    return issues


def _read_root(bundle: Path, issues: list[VerificationIssue]) -> dict[str, Any] | None:
    root_path = bundle / "release.json"
    if root_path.is_symlink():
        _issue(issues, "invalid.path", "release.json", "root manifest is a symlink")
        return None
    if not root_path.is_file():
        _issue(issues, "invalid.membership-missing", "release.json", "root manifest is absent")
        return None
    try:
        root = load_strict_canonical_json(root_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.root-syntax", "release.json", str(exc))
        return None
    if not isinstance(root, dict):
        _issue(issues, "invalid.root-syntax", "release.json", "root must be an object")
        return None
    if root.get("format") != FORMAT or root.get("formatVersion") != FORMAT_VERSION:
        _issue(
            issues,
            "invalid.format",
            "release.json",
            f"expected {FORMAT!r} version {FORMAT_VERSION!r}",
        )
    generation = bundle_generation(root)
    if generation == DOCSPEC_GENERATION:
        # Two names over one content: the state digest is the minted one, the
        # release id is derived from its hex. Both are checked, because a root
        # that carries a correct id beside a wrong state digest is a root whose
        # two names disagree about the same corpus.
        try:
            expected_state = expected_document_state_digest(root)
        except (TypeError, ValueError) as exc:
            _issue(issues, "invalid.identity", "release.json", str(exc))
        else:
            if root.get("documentStateDigest") != expected_state:
                _issue(
                    issues,
                    "invalid.identity",
                    "release.json/documentStateDigest",
                    f"expected {expected_state}",
                )
    try:
        expected = expected_release_id(root, generation=generation)
    except (TypeError, ValueError) as exc:
        _issue(issues, "invalid.identity", "release.json", str(exc))
    else:
        if root.get("releaseId") != expected:
            _issue(issues, "invalid.identity", "release.json/releaseId", f"expected {expected}")
    return root


def _validate_root_shape(
    root: Mapping[str, Any],
    schemas: Mapping[str, Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> None:
    """Check the root against the release-root schema of its own generation.

    Deferred until the members have been read, because for a predecessor bundle
    the only copy of the schema it was written against is the one it carries.
    A bundle whose release-root schema cannot be resolved has already been
    reported -- as a missing member, a broken digest, or an unregistered `$id` --
    and is not reported a second time here.
    """

    schema = schemas.get("release-root")
    if schema is not None:
        issues.extend(_schema_issues(root, schema, path="release.json"))


def _materialized_files(bundle: Path, issues: list[VerificationIssue]) -> set[str]:
    result: set[str] = set()
    for path in bundle.rglob("*"):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            _issue(issues, "invalid.path", relative, "symlinks are forbidden")
            result.add(relative)
            continue
        if path.is_file():
            result.add(relative)
    return result


def _counted_roles(generation: str) -> frozenset[str]:
    """Which member roles declare an integer ``recordCount`` in this generation.

    Restamp item 16: the rule is stated per ROLE, not per "has rows". A `schema`
    member is one document and declares null in both generations. A tabular
    member is a stream of rows in both. A `rendition` or `representation` member
    was one file per document under the predecessor and declared null; under the
    docspec generation it is a partition bucket of text bodies and carries its
    own count, exactly as the catalog's partitions do.
    """

    if generation == DOCSPEC_GENERATION:
        return frozenset({*TABULAR_ROLES, *OPAQUE_ROLES, TEXT_BODY_INDEX_ROLE})
    return frozenset(PREDECESSOR_TABULAR_ROLES)


def _validate_member_descriptor(
    member: Any, *, path: str, generation: str, issues: list[VerificationIssue]
) -> dict[str, Any] | None:
    if not isinstance(member, dict):
        _issue(issues, "invalid.schema", path, "member descriptor must be an object")
        return None
    if set(member) != MEMBER_DESCRIPTOR_FIELDS:
        _issue(issues, "invalid.schema", path, "member descriptor has an unknown or missing field")
    if not safe_object_key(member.get("objectKey")):
        _issue(issues, "invalid.path", f"{path}/objectKey", "unsafe member path")
    role = member.get("role")
    if role not in MEMBER_ROLES_BY_GENERATION[generation]:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/role",
            f"role {role!r} has no sealed schema in this generation, so its rows cannot be checked"
            if role in ALLOWED_MEMBER_ROLES
            else f"unknown role {role!r}",
        )
    if role == "schema" and member.get("mediaType") != "application/schema+json":
        _issue(issues, "invalid.schema", f"{path}/mediaType", "expected application/schema+json")
    tabular_media_type = TABULAR_MEDIA_TYPES[generation]
    if role in TABULAR_ROLES and member.get("mediaType") != tabular_media_type:
        _issue(issues, "invalid.schema", f"{path}/mediaType", f"expected {tabular_media_type}")
    if role == TEXT_BODY_INDEX_ROLE:
        if member.get("mediaType") != tabular_media_type:
            _issue(issues, "invalid.schema", f"{path}/mediaType", f"expected {tabular_media_type}")
        if canonical_schema_id(member.get("schemaId")) != SCHEMA_IDS["member-manifest"]:
            _issue(
                issues,
                "invalid.schema",
                f"{path}/schemaId",
                f"expected {SCHEMA_IDS['member-manifest']}",
            )
    if role == "representation" and member.get("mediaType") != REPRESENTATION_MEDIA_TYPE:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/mediaType",
            f"expected {REPRESENTATION_MEDIA_TYPE}",
        )
    if role in TABULAR_ROLES and canonical_schema_id(member.get("schemaId")) != SCHEMA_IDS[
        TABULAR_ROLES[role]
    ]:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/schemaId",
            f"expected {SCHEMA_IDS[TABULAR_ROLES[role]]}",
        )
    counted = _counted_roles(generation)
    record_count = member.get("recordCount")
    if role in counted:
        if not isinstance(record_count, int) or isinstance(record_count, bool):
            _issue(issues, "invalid.schema", f"{path}/recordCount", "invalid record count")
    elif record_count is not None:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/recordCount",
            f"a {role!r} member declares no row count in this generation and must declare null",
        )
    return member


def _read_member_manifest(
    bundle: Path,
    root: Mapping[str, Any],
    generation: str,
    issues: list[VerificationIssue],
) -> tuple[list[dict[str, Any]], dict[str, Path], set[str]]:
    declared = {"release.json"}
    content = root.get("content")
    if not isinstance(content, dict):
        return [], {}, declared
    reference = content.get("globalManifest")
    if not isinstance(reference, dict):
        _issue(
            issues,
            "invalid.schema",
            "release.json/content/globalManifest",
            "manifest reference must be an object",
        )
        return [], {}, declared
    if set(reference) != MANIFEST_REFERENCE_FIELDS:
        _issue(
            issues,
            "invalid.schema",
            "release.json/content/globalManifest",
            "manifest reference has an unknown or missing field",
        )
    object_key = reference.get("objectKey")
    if not safe_object_key(object_key):
        _issue(
            issues,
            "invalid.path",
            "release.json/content/globalManifest/objectKey",
            "unsafe member path",
        )
        return [], {}, declared
    declared.add(object_key)
    path = member_path(bundle, object_key)
    if path.is_symlink():
        _issue(issues, "invalid.path", object_key, "manifest is a symlink")
        return [], {}, declared
    if not path.is_file():
        _issue(issues, "invalid.membership-missing", object_key, "manifest is absent")
        return [], {}, declared
    if path.stat().st_size != reference.get("byteSize") or file_sha256(path) != reference.get(
        "sha256"
    ):
        _issue(
            issues,
            "invalid.member-digest",
            object_key,
            "manifest size or digest differs from the root reference",
        )
    try:
        manifest = load_strict_canonical_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.schema", object_key, str(exc))
        return [], {}, declared
    issues.extend(_schema_issues(manifest, _load_schema(MEMBER_MANIFEST_SCHEMA), path=object_key))
    if not isinstance(manifest, dict) or set(manifest) != SUBORDINATE_MANIFEST_FIELDS:
        _issue(issues, "invalid.schema", object_key, "invalid member manifest fields")
        return [], {}, declared
    if (
        manifest.get("format") != MEMBER_MANIFEST_FORMAT
        or manifest.get("formatVersion") != MEMBER_MANIFEST_VERSION
    ):
        _issue(issues, "invalid.schema", object_key, "unsupported member manifest format")
    expected_scope = {"kind": reference.get("scopeKind"), "id": reference.get("scopeId")}
    if manifest.get("scope") != expected_scope or manifest.get("manifestId") != reference.get(
        "manifestId"
    ):
        _issue(
            issues,
            "invalid.schema",
            object_key,
            "manifest scope differs from the root reference",
        )
    raw_members = manifest.get("members")
    if not isinstance(raw_members, list):
        _issue(issues, "invalid.schema", f"{object_key}/members", "members must be an array")
        return [], {}, declared
    object_keys = [m.get("objectKey") for m in raw_members if isinstance(m, dict)]
    if object_keys != sorted(object_keys, key=lambda key: str(key)):
        _issue(
            issues,
            "invalid.schema",
            f"{object_key}/members",
            "members must be sorted by objectKey",
        )
    members: list[dict[str, Any]] = []
    member_paths: dict[str, Path] = {}
    for index, raw_member in enumerate(raw_members):
        member = _validate_member_descriptor(
            raw_member,
            path=f"{object_key}/members/{index}",
            generation=generation,
            issues=issues,
        )
        if member is None:
            continue
        member_key = member.get("objectKey")
        if safe_object_key(member_key):
            if member_key in member_paths:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{object_key}/members/{index}/objectKey",
                    f"duplicate member {member_key}",
                )
            else:
                member_paths[member_key] = member_path(bundle, member_key)
            declared.add(member_key)
        elif isinstance(member_key, str) and member_key:
            declared.add(member_key)
        members.append(member)
    expected_counts = {
        "memberCount": len(raw_members),
        "totalByteSize": sum(
            m.get("byteSize", 0)
            for m in raw_members
            if isinstance(m, dict)
            and isinstance(m.get("byteSize"), int)
            and not isinstance(m.get("byteSize"), bool)
        ),
        "totalRecordCount": sum(
            m.get("recordCount") or 0
            for m in raw_members
            if isinstance(m, dict)
            and (
                m.get("recordCount") is None
                or (isinstance(m.get("recordCount"), int) and not isinstance(m.get("recordCount"), bool))
            )
        ),
    }
    if manifest.get("counts") != expected_counts:
        _issue(issues, "invalid.schema", f"{object_key}/counts", f"expected {expected_counts}")
    return members, member_paths, declared


def _verify_member_files(
    bundle: Path,
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    declared: set[str],
    issues: list[VerificationIssue],
) -> None:
    materialized = _materialized_files(bundle, issues)
    for object_key in sorted(declared - materialized):
        _issue(issues, "invalid.membership-missing", object_key, "declared member is absent")
    for object_key in sorted(materialized - declared):
        _issue(issues, "invalid.membership-extra", object_key, "file is not declared")
    for member in members:
        object_key = member.get("objectKey")
        if not isinstance(object_key, str) or object_key not in member_paths:
            continue
        path = member_paths[object_key]
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
            digest = file_sha256(path)
        except OSError as exc:
            _issue(issues, "invalid.membership-missing", object_key, str(exc))
            continue
        if size != member.get("byteSize") or digest != member.get("sha256"):
            _issue(
                issues,
                "invalid.member-digest",
                object_key,
                "member size or digest differs from its descriptor",
            )


def _row_subschema(schema: Any, name: str) -> dict[str, Any] | None:
    """Address one ``$defs`` entry of a carried schema as a schema in its own right.

    The wrapper keeps the whole ``$defs`` block so the entry's internal ``$ref``s
    still resolve, and carries nothing else, so none of the enclosing document's
    own keywords leak onto the row being checked.
    """

    if not isinstance(schema, Mapping):
        return None
    definitions = schema.get("$defs")
    if not isinstance(definitions, Mapping) or name not in definitions:
        return None
    return {"$defs": dict(definitions), "$ref": f"#/$defs/{name}"}


def _read_text_body_index(
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    generation: str,
    schemas: Mapping[str, Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> dict[tuple[Any, Any], Mapping[str, Any]]:
    """Load the ``text-body-index`` member, keyed by ``(family, textBodyId)``.

    Amendment A4. Digest-bucketed ``text/`` and ``blobs/`` members hold whatever
    bodies hash into them, and no other field in this format carries an offset
    into a member, so without this index one body's bytes cannot be recovered
    from a bucket it shares. With it they can, and the refusal to mint a
    multi-body bucket lifts wherever the index covers that bucket -- which needs
    no separate rule here: an indexed body is checked against its slice, an
    unindexed body against the whole member, and a body sharing an unindexed
    bucket therefore fails its own capture or representation digest.

    An indexed slice whose bytes do not digest to the row's ``sha256`` is
    ``invalid.member-digest``, exactly as a whole member's would be.
    """

    if generation != DOCSPEC_GENERATION:
        return {}
    matching = [member for member in members if member.get("role") == TEXT_BODY_INDEX_ROLE]
    if not matching:
        return {}
    if len(matching) > 1:
        _issue(
            issues,
            "invalid.schema",
            "manifests/global.json/members",
            f"at most one {TEXT_BODY_INDEX_ROLE} member is allowed",
        )
        return {}
    member = matching[0]
    object_key = str(member.get("objectKey"))
    path = member_paths.get(object_key)
    if path is None or path.is_symlink() or not path.is_file():
        return {}
    try:
        rows = load_strict_canonical_jsonl(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.schema", object_key, str(exc))
        return {}
    if len(rows) != member.get("recordCount"):
        _issue(
            issues,
            "invalid.schema",
            f"member:{object_key}/recordCount",
            f"expected {len(rows)}",
        )
    row_schema = _row_subschema(schemas.get("member-manifest"), TEXT_BODY_INDEX_ROW_DEF)
    index: dict[tuple[Any, Any], Mapping[str, Any]] = {}
    for position, row in enumerate(rows):
        row_path = f"{object_key}/{position}"
        if row_schema is not None:
            issues.extend(_schema_issues(row, row_schema, path=row_path))
        if not isinstance(row, dict):
            continue
        identity = (row.get("family"), row.get("textBodyId"))
        if identity in index:
            _issue(
                issues,
                "invalid.duplicate-identity",
                f"{row_path}/textBodyId",
                f"duplicate {identity[0]!r} slice for text body {identity[1]!r}",
            )
            continue
        index[identity] = row
        target = member_paths.get(str(row.get("member")))
        if target is None or target.is_symlink() or not target.is_file():
            _issue(
                issues,
                "invalid.membership-missing",
                f"{row_path}/member",
                "indexed member is not a declared member of this bundle",
            )
            continue
        start, length = row.get("startByte"), row.get("byteLength")
        if not isinstance(start, int) or not isinstance(length, int):
            continue
        raw = target.read_bytes()
        if start + length > len(raw):
            _issue(
                issues,
                "invalid.member-digest",
                f"{row_path}/byteLength",
                f"slice exceeds the {len(raw)}-byte member",
            )
            continue
        actual = hashlib.sha256(raw[start : start + length]).hexdigest()
        if actual != row.get("sha256"):
            _issue(
                issues,
                "invalid.member-digest",
                f"{row_path}/sha256",
                f"indexed slice digests to {actual}",
            )
    return index


def _indexed_bytes(
    path: Path,
    index: Mapping[tuple[Any, Any], Mapping[str, Any]],
    family: str,
    body_id: Any,
) -> bytes:
    """The bytes one text body owns inside a member: its slice, or the whole file.

    A bucket holding one body indexes it at offset zero for its whole length, so
    the two answers coincide and nothing changes for an unpartitioned reader.
    """

    raw = path.read_bytes()
    row = index.get((family, body_id))
    if not isinstance(row, Mapping):
        return raw
    start, length = row.get("startByte"), row.get("byteLength")
    if isinstance(start, int) and isinstance(length, int) and 0 <= start <= start + length <= len(raw):
        return raw[start : start + length]
    return raw


def _validate_capture(
    capture: Mapping[str, Any],
    member_paths: Mapping[str, Path],
    index: Mapping[tuple[Any, Any], Mapping[str, Any]],
    body_id: Any,
    path: str,
    issues: list[VerificationIssue],
) -> None:
    """Check one captured rendition against the member bytes it names."""

    resolved = member_paths.get(str(capture.get("objectKey")))
    if resolved is None or not resolved.is_file():
        _issue(
            issues,
            "invalid.capture",
            f"{path}/objectKey",
            "captured rendition is not a declared member",
        )
    else:
        blob = _indexed_bytes(resolved, index, "blob", body_id)
        actual = hashlib.sha256(blob).hexdigest()
        if actual != capture.get("sha256"):
            _issue(issues, "invalid.capture", f"{path}/sha256", f"captured bytes digest to {actual}")
        if len(blob) != capture.get("byteSize"):
            _issue(issues, "invalid.capture", f"{path}/byteSize", "captured byte size differs")
    expected = capture.get("expectedSha256")
    if isinstance(expected, str) and expected.removeprefix("sha256:") != capture.get("sha256"):
        _issue(
            issues,
            "invalid.capture",
            f"{path}/expectedSha256",
            "the catalog's expected digest does not match the captured bytes",
        )


def _validate_representation(
    representation: Mapping[str, Any],
    member_paths: Mapping[str, Path],
    index: Mapping[tuple[Any, Any], Mapping[str, Any]],
    body_id: Any,
    path: str,
    issues: list[VerificationIssue],
) -> int | None:
    """Check one selected representation and return its byte length."""

    resolved = member_paths.get(str(representation.get("objectKey")))
    if resolved is None or not resolved.is_file():
        _issue(
            issues,
            "invalid.representation",
            f"{path}/objectKey",
            "representation is not a declared member",
        )
        return None
    raw = _indexed_bytes(resolved, index, "text", body_id)
    if hashlib.sha256(raw).hexdigest() != representation.get("sha256"):
        _issue(issues, "invalid.representation", f"{path}/sha256", "representation digest differs")
    if len(raw) != representation.get("byteSize"):
        _issue(
            issues,
            "invalid.representation",
            f"{path}/byteSize",
            f"representation is {len(raw)} bytes",
        )
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _issue(
            issues,
            "invalid.representation",
            path,
            f"representation is not valid UTF-8: {exc}",
        )
    return len(raw)


def _validate_schema_set(
    root: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    generation: str,
    issues: list[VerificationIssue],
) -> dict[str, dict[str, Any]]:
    """Check the carried schema set, and hand back the bodies it resolved.

    The returned map is role -> schema body, and it is what every later row
    check validates against. Under the docspec generation the packaged body is
    the contract and the embedded copy must equal it byte for byte; under the
    predecessor generation the embedded copy IS the contract, because the bodies
    the sealed corpus was written against are not packaged anywhere else.
    """

    bodies: dict[str, dict[str, Any]] = {}
    content = root.get("content")
    schema_set = content.get("schemaSet") if isinstance(content, dict) else None
    if not isinstance(schema_set, dict):
        return bodies
    descriptors = schema_set.get("schemas")
    if not isinstance(descriptors, list):
        return bodies
    base = "release.json/content/schemaSet"
    if len(declared_generations(root)) > 1:
        _issue(
            issues,
            "invalid.schema",
            f"{base}/schemas",
            "schema identifiers mix minting generations",
        )
    ids = [item.get("schemaId") for item in descriptors if isinstance(item, dict)]
    if ids != sorted(ids, key=lambda value: str(value)):
        _issue(issues, "invalid.schema", f"{base}/schemas", "schemas must be sorted by schemaId")
    try:
        expected_set_id = f"urn:spicy:schema-set:v1:{canonical_sha256(descriptors)}"
    except (TypeError, ValueError) as exc:
        _issue(issues, "invalid.schema", base, str(exc))
    else:
        if schema_set.get("schemaSetId") != expected_set_id:
            _issue(issues, "invalid.schema", f"{base}/schemaSetId", f"expected {expected_set_id}")
    schema_members = {
        member["schemaId"]: member
        for member in members
        if member.get("role") == "schema" and isinstance(member.get("schemaId"), str)
    }
    seen_roles: dict[str, int] = {}
    for index, descriptor in enumerate(descriptors):
        path = f"{base}/schemas/{index}"
        if not isinstance(descriptor, dict):
            continue
        schema_id = descriptor.get("schemaId")
        roles = descriptor.get("roles")
        role = roles[0] if isinstance(roles, list) and len(roles) == 1 else None
        if role is None or SCHEMA_IDS.get(role) != canonical_schema_id(schema_id):
            _issue(
                issues,
                "invalid.schema",
                f"{path}/roles",
                f"role {role!r} must resolve to the registered schema for {schema_id!r}",
            )
            continue
        seen_roles[role] = seen_roles.get(role, 0) + 1
        member = schema_members.get(schema_id)
        if member is None:
            _issue(
                issues,
                "invalid.membership-missing",
                f"{path}/schemaId",
                "schema descriptor has no schema member",
            )
            continue
        if member.get("sha256") != descriptor.get("schemaSha256"):
            _issue(
                issues,
                "invalid.member-digest",
                f"{path}/schemaSha256",
                "schema descriptor digest differs from the member",
            )
        resolved = member_paths.get(str(member.get("objectKey")))
        if resolved is None or not resolved.is_file():
            continue
        try:
            schema = json.loads(resolved.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)
        except (OSError, ValueError, jsonschema.SchemaError) as exc:
            _issue(issues, "invalid.schema", str(member.get("objectKey")), str(exc))
            continue
        if schema.get("$id") != schema_id:
            _issue(
                issues,
                "invalid.schema",
                str(member.get("objectKey")),
                "$id differs from the descriptor",
            )
            continue
        if generation == DOCSPEC_GENERATION and schema != _load_schema(SCHEMA_FILES[role]):
            # The packaged schema is the docspec generation. A bundle may carry
            # its own copy -- that is what makes it portable -- but a copy that
            # says something else is a bundle checked against a contract nobody
            # registered.
            _issue(
                issues,
                "invalid.schema",
                str(member.get("objectKey")),
                f"embedded schema differs from the registered schema for role {role!r}",
            )
            continue
        bodies[role] = schema
    for role in sorted(GENERATION_SCHEMA_ROLES[generation]):
        if seen_roles.get(role) != 1:
            _issue(issues, "invalid.schema", f"{base}/schemas", f"role {role!r} must resolve exactly once")
    return bodies


def _read_rows(
    role: str,
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    generation: str,
    schemas: Mapping[str, Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Load one tabular member's rows, or ``None`` when they cannot be trusted.

    Restamp item 11: the docspec generation carries these members as JSONL, one
    canonical-JSON record per newline-terminated line, so a consumer streams the
    rows instead of parsing a whole file to reach the first one. The predecessor
    corpus carries a single JSON array. Which reader runs is read off the
    generation, never sniffed from the bytes.
    """

    matching = [member for member in members if member.get("role") == role]
    if len(matching) != 1:
        _issue(
            issues,
            "invalid.schema",
            "manifests/global.json/members",
            f"exactly one {role} member is required",
        )
        return None, role
    member = matching[0]
    object_key = str(member.get("objectKey"))
    path = member_paths.get(object_key)
    if path is None or path.is_symlink() or not path.is_file():
        return None, object_key
    reader = (
        load_strict_canonical_jsonl
        if generation == DOCSPEC_GENERATION
        else load_strict_canonical_json
    )
    try:
        rows = reader(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.schema", object_key, str(exc))
        return None, object_key
    if not isinstance(rows, list):
        _issue(issues, "invalid.schema", object_key, f"{role} must be an array")
        return None, object_key
    if len(rows) != member.get("recordCount"):
        _issue(
            issues,
            "invalid.schema",
            f"member:{object_key}/recordCount",
            f"expected {len(rows)}",
        )
    schema = schemas.get(TABULAR_ROLES[role])
    if schema is not None:
        for index, row in enumerate(rows):
            issues.extend(_schema_issues(row, schema, path=f"{object_key}/{index}"))
    return [row for row in rows if isinstance(row, dict)], object_key


def _validate_dispositions(
    dispositions: Sequence[Mapping[str, Any]], object_key: str, issues: list[VerificationIssue]
) -> None:
    seen_items: set[str] = set()
    seen_documents: set[str] = set()
    for index, row in enumerate(dispositions):
        path = f"{object_key}/{index}"
        source_item_id = row.get("sourceItemId")
        if isinstance(source_item_id, str):
            if source_item_id in seen_items:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/sourceItemId",
                    f"duplicate sourceItemId {source_item_id}",
                )
            seen_items.add(source_item_id)
        disposition = row.get("catalogDisposition")
        if disposition in NON_SELECTED_DISPOSITIONS:
            for field in ("reasonCode", "reason"):
                if not row.get(field):
                    _issue(
                        issues,
                        "invalid.disposition",
                        f"{path}/{field}",
                        f"projected disposition {disposition!r} requires a {field}",
                    )
        if disposition == "selected":
            version_id = row.get("documentVersionId")
            if isinstance(version_id, str):
                if version_id in seen_documents:
                    _issue(
                        issues,
                        "invalid.duplicate-identity",
                        f"{path}/documentVersionId",
                        f"duplicate documentVersionId {version_id}",
                    )
                seen_documents.add(version_id)


def _validate_documents(
    documents: Sequence[Mapping[str, Any]],
    dispositions: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    slices: Mapping[tuple[Any, Any], Mapping[str, Any]],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> dict[str, int]:
    """Check captures and representations. Returns representation byte sizes.

    The returned sizes are keyed by ``key`` -- the field structure and segments
    hang off in this generation -- because that is what the later range checks
    resolve against. For a document body the two names hold the same value; the
    declared one is the one read.
    """

    selected = {
        row["sourceItemId"]: row
        for row in dispositions
        if row.get("catalogDisposition") == "selected" and isinstance(row.get("sourceItemId"), str)
    }
    sizes: dict[str, int] = {}
    seen_versions: set[str] = set()
    for index, document in enumerate(documents):
        path = f"{object_key}/{index}"
        version_id = document.get("documentVersionId")
        if isinstance(version_id, str):
            if version_id in seen_versions:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/documentVersionId",
                    f"duplicate documentVersionId {version_id}",
                )
            seen_versions.add(version_id)

        source_item_id = document.get("sourceItemId")
        projection = selected.get(source_item_id) if isinstance(source_item_id, str) else None
        if projection is None:
            _issue(
                issues,
                "invalid.join",
                f"{path}/sourceItemId",
                "document has no selected disposition row",
            )
        else:
            for field in ("documentId", "sourceIssuedVersion"):
                if document.get(field) != projection.get(field):
                    _issue(
                        issues,
                        "invalid.join",
                        f"{path}/{field}",
                        f"differs from the disposition projection ({projection.get(field)!r})",
                    )
            if projection.get("documentVersionId") != version_id:
                _issue(
                    issues,
                    "invalid.join",
                    f"{path}/documentVersionId",
                    "disposition projection names a different document version",
                )

        body_id = document.get(key)
        capture = document.get("capture")
        if isinstance(capture, Mapping):
            _validate_capture(
                capture, member_paths, slices, body_id, f"{path}/capture", issues
            )

        representation = document.get("representation")
        if isinstance(representation, Mapping):
            size = _validate_representation(
                representation, member_paths, slices, body_id, f"{path}/representation", issues
            )
            if size is not None and isinstance(body_id, str):
                sizes[body_id] = size
    return sizes


def _validate_attachments(
    attachments: Sequence[Mapping[str, Any]],
    owners: Mapping[str, str],
    member_paths: Mapping[str, Path],
    index: Mapping[tuple[Any, Any], Mapping[str, Any]],
    object_key: str,
    issues: list[VerificationIssue],
) -> dict[str, int]:
    """Check the attachment rows, their renditions, and their accounting.

    Amendment A1 governs the identity: ``attachmentId`` is minted over
    ``{ownerTextBodyId, ownerKind, attachmentIdentity}`` and nothing else, so
    re-enumerating an unchanged owner re-mints the same id and the row that
    groups M renditions is not renamed by any one of them.

    Attachments are not members of ``U``, so nothing here blocks a build for a
    rendition that failed: the build fails when an enumerated attachment has no
    row, and a row that honestly says it could not be captured is the accounting
    working. What IS refused is a row that claims text without carrying it, or
    carries text without saying which rendition produced it.
    """

    sizes: dict[str, int] = {}
    seen: set[str] = set()
    for position, attachment in enumerate(attachments):
        path = f"{object_key}/{position}"
        attachment_id = attachment.get("attachmentId")
        if isinstance(attachment_id, str):
            if attachment_id in seen:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/attachmentId",
                    f"duplicate attachmentId {attachment_id}",
                )
            seen.add(attachment_id)
        owner_id = attachment.get("ownerTextBodyId")
        owner_kind = attachment.get("ownerKind")
        if isinstance(owner_id, str) and isinstance(owner_kind, str):
            expected_id = stable_urn(
                "document-release-attachment",
                {
                    "attachmentIdentity": attachment.get("attachmentIdentity"),
                    "ownerKind": owner_kind,
                    "ownerTextBodyId": owner_id,
                },
                version=2,
            )
            if attachment_id != expected_id:
                _issue(
                    issues,
                    "invalid.identity",
                    f"{path}/attachmentId",
                    f"expected {expected_id}",
                )
            owned = owners.get(owner_id)
            if owned is None:
                _issue(
                    issues,
                    "invalid.join",
                    f"{path}/ownerTextBodyId",
                    "attachment names no text body in this release",
                )
            elif owned != owner_kind:
                _issue(
                    issues,
                    "invalid.join",
                    f"{path}/ownerKind",
                    f"owner {owner_id!r} is a {owned!r}, not a {owner_kind!r}",
                )

        renditions = attachment.get("renditions")
        captured: list[int] = []
        if isinstance(renditions, list):
            ordinals: list[int] = []
            for order, rendition in enumerate(renditions):
                if not isinstance(rendition, Mapping):
                    continue
                sub_path = f"{path}/renditions/{order}"
                if isinstance(rendition.get("renditionOrdinal"), int):
                    ordinals.append(rendition["renditionOrdinal"])
                disposition = rendition.get("attachmentDisposition")
                if disposition == "text-captured":
                    captured.append(order)
                elif disposition in ATTACHMENT_DISPOSITIONS:
                    for field in ("reasonCode", "reason"):
                        if not rendition.get(field):
                            _issue(
                                issues,
                                "invalid.disposition",
                                f"{sub_path}/{field}",
                                f"attachment disposition {disposition!r} requires a {field}",
                            )
                capture = rendition.get("capture")
                if isinstance(capture, Mapping):
                    _validate_capture(
                        capture,
                        member_paths,
                        index,
                        # The index addresses one selected rendition per text
                        # body, because that is the slice its row shape names.
                        # Whether an attachment's OTHER renditions are kept as
                        # blobs at all is a recorded open question, so they are
                        # read against the whole member rather than borrowing a
                        # slice that names different bytes.
                        attachment.get("textBodyId") if disposition == "text-captured" else None,
                        f"{sub_path}/capture",
                        issues,
                    )
            if sorted(ordinals) != list(range(len(ordinals))):
                _issue(
                    issues,
                    "invalid.schema",
                    f"{path}/renditions",
                    f"rendition ordinals must be dense and zero-based, found {sorted(ordinals)}",
                )

        # One attachment is one text body, and a text body has exactly one
        # selected representation, so at most one rendition of it can have been
        # text-captured. `textBodyId` names that body and EQUALS `attachmentId`.
        body_id = attachment.get("textBodyId")
        if len(captured) > 1:
            _issue(
                issues,
                "invalid.duplicate-identity",
                f"{path}/renditions",
                f"{len(captured)} renditions claim text-captured; one attachment is one text body",
            )
        if body_id is None:
            if captured:
                _issue(
                    issues,
                    "invalid.disposition",
                    f"{path}/textBodyId",
                    "a text-captured rendition means this attachment is a text body",
                )
        else:
            if not captured:
                _issue(
                    issues,
                    "invalid.disposition",
                    f"{path}/textBodyId",
                    "an attachment carrying text must name the rendition that produced it",
                )
            if body_id != attachment_id:
                _issue(
                    issues,
                    "invalid.identity",
                    f"{path}/textBodyId",
                    f"expected {attachment_id!r}",
                )
        representation = attachment.get("representation")
        if isinstance(representation, Mapping):
            size = _validate_representation(
                representation, member_paths, index, body_id, f"{path}/representation", issues
            )
            if size is not None and isinstance(body_id, str):
                sizes[body_id] = size
        elif body_id is not None:
            _issue(
                issues,
                "invalid.representation",
                f"{path}/representation",
                "an attachment carrying text must carry its selected representation",
            )
    return sizes


def _validate_comments(
    comments: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    index: Mapping[tuple[Any, Any], Mapping[str, Any]],
    object_key: str,
    issues: list[VerificationIssue],
) -> dict[str, int]:
    """Check the comment rows: identity, ownership, and the inherited refusal.

    ``commentId`` equals the catalog's own comment ``sourceRecordId``, so the
    release cannot disagree with the selection it inherits, and ``textBodyId``
    equals it in turn. A comment's owner is exactly one document. The selection
    policy itself is projected verbatim and checked by schema; the tie REFUSAL
    it carries is the upstream owner's, and a repeated comment id here would be
    DocSpec resolving a tie the source owner refused to resolve, so a repeat is
    a duplicate identity rather than a value this release picks between.
    """

    sizes: dict[str, int] = {}
    document_ids = {
        document["documentId"]
        for document in documents
        if isinstance(document.get("documentId"), str)
    }
    seen: set[str] = set()
    for position, comment in enumerate(comments):
        path = f"{object_key}/{position}"
        comment_id = comment.get("commentId")
        if isinstance(comment_id, str):
            if comment_id in seen:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/commentId",
                    f"duplicate commentId {comment_id}",
                )
            seen.add(comment_id)
        body_id = comment.get("textBodyId")
        if body_id != comment_id:
            _issue(issues, "invalid.identity", f"{path}/textBodyId", f"expected {comment_id!r}")
        if comment.get("documentId") not in document_ids:
            _issue(
                issues,
                "invalid.join",
                f"{path}/documentId",
                "comment names no document in this release",
            )
        capture = comment.get("capture")
        if isinstance(capture, Mapping):
            _validate_capture(
                capture, member_paths, index, body_id, f"{path}/capture", issues
            )
        representation = comment.get("representation")
        if isinstance(representation, Mapping):
            size = _validate_representation(
                representation, member_paths, index, body_id, f"{path}/representation", issues
            )
            if size is not None and isinstance(body_id, str):
                sizes[body_id] = size
    return sizes


def _validate_structure(
    nodes: Sequence[Mapping[str, Any]],
    sizes: Mapping[str, int],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        path = f"{object_key}/{index}"
        node_id = node.get("structuralNodeId")
        if isinstance(node_id, str):
            if node_id in by_id:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/structuralNodeId",
                    f"duplicate structuralNodeId {node_id}",
                )
            else:
                by_id[node_id] = dict(node)
    sibling_ordinals: dict[tuple[str, Any], list[int]] = {}
    for index, node in enumerate(nodes):
        path = f"{object_key}/{index}"
        version_id = node.get(key)
        start, end = node.get("representationStart"), node.get("representationEnd")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if end < start:
            _issue(issues, "invalid.structure", f"{path}/representationEnd", "range is inverted")
        size = sizes.get(str(version_id))
        if size is None:
            _issue(
                issues,
                "invalid.structure",
                f"{path}/{key}",
                "node names no text body in this release",
            )
        elif end > size:
            _issue(
                issues,
                "invalid.structure",
                f"{path}/representationEnd",
                f"range exceeds the {size}-byte representation",
            )
        parent_id = node.get("structuralParentId")
        if parent_id is not None:
            parent = by_id.get(str(parent_id))
            if parent is None:
                _issue(
                    issues,
                    "invalid.structure",
                    f"{path}/structuralParentId",
                    f"parent {parent_id!r} does not resolve",
                )
            else:
                if parent.get(key) != version_id:
                    _issue(
                        issues,
                        "invalid.structure",
                        f"{path}/structuralParentId",
                        "parent belongs to a different text body",
                    )
                if isinstance(parent.get("depth"), int) and node.get("depth") != parent["depth"] + 1:
                    _issue(
                        issues,
                        "invalid.structure",
                        f"{path}/depth",
                        f"expected {parent['depth'] + 1}",
                    )
                p_start, p_end = parent.get("representationStart"), parent.get("representationEnd")
                if isinstance(p_start, int) and isinstance(p_end, int) and not (
                    p_start <= start and end <= p_end
                ):
                    _issue(
                        issues,
                        "invalid.structure",
                        f"{path}/representationStart",
                        "range is not contained in its parent",
                    )
        elif node.get("depth") != 0:
            _issue(issues, "invalid.structure", f"{path}/depth", "a root node has depth 0")
        ordinal = node.get("ordinal")
        if isinstance(ordinal, int):
            sibling_ordinals.setdefault((str(version_id), parent_id), []).append(ordinal)
    for (version_id, parent_id), ordinals in sorted(
        sibling_ordinals.items(), key=lambda item: (item[0][0], str(item[0][1]))
    ):
        if sorted(ordinals) != list(range(len(ordinals))):
            _issue(
                issues,
                "invalid.structure",
                f"{object_key}:{version_id}:{parent_id}",
                f"sibling ordinals must be dense and zero-based, found {sorted(ordinals)}",
            )
    return by_id


def _validate_segments(
    segments: Sequence[Mapping[str, Any]],
    nodes: Mapping[str, Mapping[str, Any]],
    renditions: Mapping[Any, Mapping[str, Any]],
    sizes: Mapping[str, int],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> None:
    seen: set[str] = set()
    ordinals: dict[str, list[int]] = {}
    for index, segment in enumerate(segments):
        path = f"{object_key}/{index}"
        segment_id = segment.get("segmentId")
        if isinstance(segment_id, str):
            if segment_id in seen:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/segmentId",
                    f"duplicate segmentId {segment_id}",
                )
            seen.add(segment_id)
        version_id = str(segment.get(key))
        start, end = segment.get("representationStart"), segment.get("representationEnd")
        size = sizes.get(version_id)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if end <= start:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/representationEnd",
                "range is empty or inverted",
            )
        if size is None:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/{key}",
                "segment names no text body in this release",
            )
        elif end > size:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/representationEnd",
                f"range exceeds the {size}-byte representation",
            )
        parent = nodes.get(str(segment.get("structuralParentId")))
        if parent is None:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/structuralParentId",
                "structural parent does not resolve",
            )
        else:
            if parent.get(key) != segment.get(key):
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/structuralParentId",
                    "structural parent belongs to a different text body",
                )
            p_start, p_end = parent.get("representationStart"), parent.get("representationEnd")
            if isinstance(p_start, int) and isinstance(p_end, int) and not (
                p_start <= start and end <= p_end
            ):
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/representationStart",
                    "range is not contained in its structural parent",
                )
            expected_path = _heading_path(parent, nodes)
            if segment.get("headingPath") != expected_path:
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/headingPath",
                    f"expected {expected_path}",
                )
        evidence = segment.get("evidence")
        capture = renditions.get(segment.get(key))
        if isinstance(evidence, Mapping) and isinstance(capture, Mapping):
            if evidence.get("renditionSha256") != capture.get("sha256"):
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/evidence/renditionSha256",
                    "evidence names bytes that are not this document's captured rendition",
                )
            e_start, e_end = evidence.get("start"), evidence.get("end")
            rendition_size = capture.get("byteSize")
            if isinstance(e_start, int) and isinstance(e_end, int):
                if e_end <= e_start:
                    _issue(
                        issues,
                        "invalid.segment",
                        f"{path}/evidence/end",
                        "evidence range is empty or inverted",
                    )
                elif isinstance(rendition_size, int) and e_end > rendition_size:
                    _issue(
                        issues,
                        "invalid.segment",
                        f"{path}/evidence/end",
                        f"evidence exceeds the {rendition_size}-byte rendition",
                    )
        if isinstance(segment.get("ordinal"), int):
            ordinals.setdefault(version_id, []).append(segment["ordinal"])
    for version_id, values in sorted(ordinals.items()):
        if sorted(values) != list(range(len(values))):
            _issue(
                issues,
                "invalid.segment",
                f"{object_key}:{version_id}",
                f"segment ordinals must be dense and zero-based, found {sorted(values)}",
            )


def _heading_path(
    node: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Heading text from the document root down to ``node``, outermost first."""

    chain: list[str] = []
    current: Mapping[str, Any] | None = node
    guard = 0
    while current is not None and guard < 4096:
        guard += 1
        text = current.get("headingText")
        if isinstance(text, str) and text:
            chain.append(text)
        parent_id = current.get("structuralParentId")
        current = nodes.get(str(parent_id)) if parent_id is not None else None
    chain.reverse()
    return chain


def _validate_coverage(
    documents: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    sizes: Mapping[str, int],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> None:
    """Every visible-text byte is segmented or explicitly excluded, never both."""

    for index, document in enumerate(documents):
        path = f"{object_key}/{index}"
        version_id = document.get(key)
        size = sizes.get(str(version_id))
        if size is None:
            continue
        segment_ranges = [
            (segment["representationStart"], segment["representationEnd"])
            for segment in segments
            if segment.get(key) == version_id
            and isinstance(segment.get("representationStart"), int)
            and isinstance(segment.get("representationEnd"), int)
        ]
        excluded_ranges = [
            (item["start"], item["end"])
            for item in document.get("excludedRanges") or []
            if isinstance(item, Mapping)
            and isinstance(item.get("start"), int)
            and isinstance(item.get("end"), int)
        ]
        if not segment_ranges:
            _issue(
                issues,
                "invalid.coverage",
                f"{path}/documentVersionId",
                "every document requires at least one search segment",
            )
        segment_cover = _interval_union(segment_ranges)
        excluded_cover = _interval_union(excluded_ranges)
        for start, end in excluded_cover:
            for other_start, other_end in segment_cover:
                if start < other_end and other_start < end:
                    _issue(
                        issues,
                        "invalid.coverage",
                        f"{path}/excludedRanges",
                        f"excluded range [{start}, {end}) overlaps a search segment",
                    )
                    break
        combined = _interval_union([*segment_ranges, *excluded_ranges])
        if combined != ([(0, size)] if size else []):
            _issue(
                issues,
                "invalid.coverage",
                f"{path}/representation",
                f"segments plus exclusions must tile [0, {size}); found {combined}",
            )


def _validate_root_bindings(
    root: Mapping[str, Any],
    dispositions: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]],
    documents_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> None:
    content = root.get("content")
    if not isinstance(content, Mapping):
        return

    selected_ids = [
        row["sourceItemId"]
        for row in dispositions
        if row.get("catalogDisposition") == "selected" and isinstance(row.get("sourceItemId"), str)
    ]
    version_ids = [
        document["documentVersionId"]
        for document in documents
        if isinstance(document.get("documentVersionId"), str)
    ]
    segment_ids = [
        segment["segmentId"] for segment in segments if isinstance(segment.get("segmentId"), str)
    ]
    pairs = [
        [document["sourceItemId"], document["documentVersionId"]]
        for document in documents
        if isinstance(document.get("sourceItemId"), str)
        and isinstance(document.get("documentVersionId"), str)
    ]
    joined = [
        document
        for document in documents
        if isinstance(document.get("sourceItemId"), str)
        and isinstance(document.get("documentId"), str)
        and isinstance(document.get("documentVersionId"), str)
    ]
    text_bodies = [
        {"textBodyId": document[key], "textKind": document["textKind"]}
        for document in documents
        if isinstance(document.get(key), str) and isinstance(document.get("textKind"), str)
    ]
    # One text pipeline, three kinds: the text-body set spans every kind that
    # carries text. An attachment whose renditions all failed carries none and
    # is not a member of it -- it is accounted in its own row, not here.
    text_bodies += [
        {"textBodyId": row["textBodyId"], "textKind": row["textKind"]}
        for row in [*attachments, *comments]
        if isinstance(row.get("textBodyId"), str) and isinstance(row.get("textKind"), str)
    ]
    attachment_ids = [
        row["attachmentId"] for row in attachments if isinstance(row.get("attachmentId"), str)
    ]
    comment_ids = [
        row["commentId"] for row in comments if isinstance(row.get("commentId"), str)
    ]
    generation = bundle_generation(root)

    catalog = content.get("sourceCatalog")
    if isinstance(catalog, Mapping):
        # The predecessor pin carries the catalog's own `selectedSourceSetDigest`
        # and is checked against it. The docspec-generation pin is
        # `{catalogId, catalogDigest}` (Decision 0001, restamp item 9) and
        # carries no set digest at all, so there is nothing for the release to
        # agree with; its own value is checked below, against the members.
        if generation == PREDECESSOR_GENERATION:
            expected_selected = source_set_digest(selected_ids)
            if catalog.get("selectedSourceSetDigest") != expected_selected:
                _issue(
                    issues,
                    "invalid.source-catalog-pin",
                    "release.json/content/sourceCatalog/selectedSourceSetDigest",
                    "the pinned catalog's selected set does not equal this release's selected rows",
                )
            if content.get("selectedSourceSetDigest") != catalog.get("selectedSourceSetDigest"):
                _issue(
                    issues,
                    "invalid.source-catalog-pin",
                    "release.json/content/selectedSourceSetDigest",
                    "release and pinned catalog disagree on the selected source set",
                )
        pinned = (
            catalog.get("catalogId")
            if generation == DOCSPEC_GENERATION
            else catalog.get("releaseId")
        )
        for index, document in enumerate(documents):
            capture = document.get("capture")
            if isinstance(capture, Mapping) and capture.get("catalogReleaseId") != pinned:
                _issue(
                    issues,
                    "invalid.source-catalog-pin",
                    f"{documents_key}/{index}/capture/catalogReleaseId",
                    f"capture names a different catalog release than the root pin {pinned!r}",
                )

    # One fact, two minting rules. The sealed corpus is plain sorted-set digests
    # and a list digest over the pairs; the docspec generation is framed digests
    # under the domains Decision 0001 declares, the pair digest among them.
    digest_plan: tuple[tuple[str, Callable[[], str]], ...]
    if generation == DOCSPEC_GENERATION:
        digest_plan = (
            (
                "selectedSourceSetDigest",
                lambda: framed_set_digest(SELECTED_SOURCE_SET_DOMAIN, joined),
            ),
            (
                "documentVersionSetDigest",
                lambda: framed_set_digest(
                    "docspec-document-version-set/2",
                    [{"documentVersionId": value} for value in version_ids],
                ),
            ),
            (
                "segmentSetDigest",
                lambda: framed_set_digest(
                    "docspec-segment-set/2",
                    [{"segmentId": value} for value in segment_ids],
                ),
            ),
            (
                "sourceDocumentMappingDigest",
                lambda: framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, joined),
            ),
            (
                "textBodySetDigest",
                lambda: framed_set_digest("docspec-text-body-set/2", text_bodies),
            ),
            # A release with none of a kind streams the empty set rather than
            # omitting the digest: a zero is written, never omitted.
            (
                "attachmentSetDigest",
                lambda: framed_set_digest(
                    "docspec-attachment-set/2",
                    [{"attachmentId": value} for value in attachment_ids],
                ),
            ),
            (
                "commentSetDigest",
                lambda: framed_set_digest(
                    "docspec-comment-set/2",
                    [{"commentId": value} for value in comment_ids],
                ),
            ),
        )
    else:
        digest_plan = (
            ("selectedSourceSetDigest", lambda: source_set_digest(selected_ids)),
            ("documentVersionSetDigest", lambda: source_set_digest(version_ids)),
            ("segmentSetDigest", lambda: source_set_digest(segment_ids)),
            ("sourceDocumentMappingDigest", lambda: mapping_digest(pairs)),
        )
    for field, compute in digest_plan:
        try:
            expected = compute()
        except (TypeError, ValueError) as exc:
            _issue(issues, "invalid.set-digest", f"release.json/content/{field}", str(exc))
            continue
        if content.get(field) != expected:
            _issue(
                issues,
                "invalid.set-digest",
                f"release.json/content/{field}",
                f"expected {expected}",
            )

    receipt = content.get("joinReceipt")
    if isinstance(receipt, Mapping):
        if receipt.get("mappingDigest") != content.get("sourceDocumentMappingDigest"):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt/mappingDigest",
                "join receipt does not seal the release's mapping digest",
            )
        if receipt.get("selectedSourceItemCount") != len(selected_ids):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt/selectedSourceItemCount",
                f"expected {len(selected_ids)}",
            )
        if receipt.get("documentVersionCount") != len(version_ids):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt/documentVersionCount",
                f"expected {len(version_ids)}",
            )
        if len(selected_ids) != len(version_ids) or len(set(selected_ids)) != len(
            set(version_ids)
        ):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt",
                "the source-to-document join is not one-to-one",
            )

    expected_counts = derive_counts(
        dispositions,
        documents,
        nodes,
        segments,
        member_count=len(members),
        total_member_byte_size=sum(
            member.get("byteSize", 0)
            for member in members
            if isinstance(member.get("byteSize"), int) and not isinstance(member.get("byteSize"), bool)
        ),
        attachments=attachments,
        comments=comments,
        generation=generation,
    )
    if content.get("counts") != expected_counts:
        _issue(issues, "invalid.counts", "release.json/content/counts", f"expected {expected_counts}")
    expected_coverage = derive_coverage(
        dispositions,
        documents,
        segments,
        key=key,
        attachments=attachments,
        comments=comments,
    )
    if content.get("coverage") != expected_coverage:
        _issue(
            issues,
            "invalid.coverage",
            "release.json/content/coverage",
            f"expected {expected_coverage}",
        )
    _validate_coverage_identity(content, generation, issues)


def _validate_coverage_identity(
    content: Mapping[str, Any], generation: str, issues: list[VerificationIssue]
) -> None:
    """``segmented + excluded == representation``, per kind and in aggregate.

    Amendment A2 states the identity twice on purpose. An aggregate that
    balances while one kind's does not is a hole in one kind hidden by a surplus
    in another, and the whole point of the per-kind breakdown is that such a
    hole has nowhere to hide. The per-kind half is the docspec generation's:
    the sealed corpus carries no `perKind` to check.
    """

    def holds(totals: Any) -> bool:
        values = [
            totals.get(field)
            for field in ("segmentedByteTotal", "excludedByteTotal", "representationByteTotal")
        ]
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return True
        segmented, excluded, representation = values
        return segmented + excluded == representation

    coverage = content.get("coverage")
    if isinstance(coverage, Mapping) and not holds(coverage):
        _issue(
            issues,
            "invalid.coverage",
            "release.json/content/coverage",
            "segmentedByteTotal + excludedByteTotal must equal representationByteTotal",
        )
    if generation != DOCSPEC_GENERATION:
        return
    counts = content.get("counts")
    per_kind = counts.get("perKind") if isinstance(counts, Mapping) else None
    if not isinstance(per_kind, Mapping):
        return
    for kind in TEXT_KINDS:
        totals = per_kind.get(kind)
        if isinstance(totals, Mapping) and not holds(totals):
            _issue(
                issues,
                "invalid.coverage",
                f"release.json/content/counts/perKind/{kind}",
                "segmentedByteTotal + excludedByteTotal must equal representationByteTotal",
            )


def verify_document_release(bundle: Path) -> VerificationResult:
    """Verify one materialized ``DocumentRelease`` v2 bundle."""

    bundle = Path(bundle)
    issues: list[VerificationIssue] = []
    root = _read_root(bundle, issues)
    if root is None:
        return VerificationResult(None, tuple(issues))
    # One reading of the bundle's own declaration, threaded through every rule
    # below, so a bundle can never be parsed under one generation and judged
    # under the other.
    generation = bundle_generation(root)
    key = TEXT_BODY_KEYS[generation]
    members, member_paths, declared = _read_member_manifest(bundle, root, generation, issues)
    _verify_member_files(bundle, members, member_paths, declared, issues)
    schemas = _validate_schema_set(root, members, member_paths, generation, issues)
    _validate_root_shape(root, schemas, issues)

    def rows(role: str) -> tuple[list[dict[str, Any]] | None, str]:
        return _read_rows(role, members, member_paths, generation, schemas, issues)

    dispositions, dispositions_key = rows("source-dispositions")
    documents, documents_key = rows("documents")
    nodes, nodes_key = rows("structural-nodes")
    segments, segments_key = rows("search-segments")
    # The two members restamp item 2 sealed. They belong to the docspec
    # generation alone: a predecessor bundle declaring one would be declaring a
    # member only another generation's schemas could judge, which the role
    # vocabulary already refuses.
    if generation == DOCSPEC_GENERATION:
        attachments, attachments_key = rows("attachments")
        comments, comments_key = rows("comments")
    else:
        attachments, attachments_key = [], "data/attachments.jsonl"
        comments, comments_key = [], "data/comments.jsonl"
    slices = _read_text_body_index(members, member_paths, generation, schemas, issues)

    if dispositions is not None:
        _validate_dispositions(dispositions, dispositions_key, issues)
    sizes: dict[str, int] = {}
    if documents is not None and dispositions is not None:
        sizes = _validate_documents(
            documents, dispositions, member_paths, slices, documents_key, key, issues
        )
    if comments is not None and documents is not None:
        sizes |= _validate_comments(
            comments, documents, member_paths, slices, comments_key, issues
        )
    if attachments is not None and documents is not None and comments is not None:
        owners = {
            **{
                document[key]: "document-body"
                for document in documents
                if isinstance(document.get(key), str)
            },
            **{
                comment["textBodyId"]: "comment"
                for comment in comments
                if isinstance(comment.get("textBodyId"), str)
            },
        }
        sizes |= _validate_attachments(
            attachments, owners, member_paths, slices, attachments_key, issues
        )
    node_index: dict[str, dict[str, Any]] = {}
    if nodes is not None:
        node_index = _validate_structure(nodes, sizes, nodes_key, key, issues)
    if segments is not None and documents is not None:
        # Evidence is checked against the captured bytes of the body the segment
        # names, whichever kind that body is: one text pipeline, three kinds.
        renditions: dict[Any, Mapping[str, Any]] = {}
        for row, body_key in (
            *((document, key) for document in documents),
            *((comment, "textBodyId") for comment in comments or []),
        ):
            if isinstance(row.get("capture"), Mapping):
                renditions[row.get(body_key)] = row["capture"]
        for attachment in attachments or []:
            body_id = attachment.get("textBodyId")
            for rendition in attachment.get("renditions") or []:
                if (
                    isinstance(rendition, Mapping)
                    and rendition.get("attachmentDisposition") == "text-captured"
                    and isinstance(rendition.get("capture"), Mapping)
                ):
                    renditions[body_id] = rendition["capture"]
        _validate_segments(segments, node_index, renditions, sizes, segments_key, key, issues)
        _validate_coverage(documents, segments, sizes, documents_key, key, issues)
    if None not in (dispositions, documents, attachments, comments, nodes, segments):
        _validate_root_bindings(
            root,
            dispositions,
            documents,
            attachments,
            comments,
            nodes,
            segments,
            members,
            documents_key,
            key,
            issues,
        )

    release_id = root.get("releaseId")
    return VerificationResult(
        release_id if isinstance(release_id, str) else None, tuple(issues)
    )


def verify_corpus(corpus_file: Path) -> list[dict[str, Any]]:
    """Verify every sealed fixture and return one row per case.

    The corpus path is a required argument: the fixture root is a test input,
    not a packaged one, so an installed DocSpec cannot name a default for it.
    """

    corpus = json.loads(Path(corpus_file).read_text(encoding="utf-8"))
    fixture_root = Path(corpus_file).parent
    rows: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        bundle = fixture_root / case["bundle"]
        observed_tree = tree_digest(bundle) if bundle.is_dir() else None
        result = (
            verify_document_release(bundle) if bundle.is_dir() else VerificationResult(None, ())
        )
        rows.append(
            {
                "name": case["name"],
                "bundle": case["bundle"],
                "sealed": observed_tree == case["treeSha256"],
                "expectedCode": case["expectedCode"],
                "observedCode": result.code if bundle.is_dir() else "absent",
                "expectedPath": case["expectedPath"],
                "observedPath": result.path if bundle.is_dir() else None,
                "issues": [str(issue) for issue in result.issues],
            }
        )
    return rows


__all__ = [
    "ATTACHMENTS_SCHEMA",
    "ATTACHMENT_DISPOSITIONS",
    "CATALOG_DISPOSITIONS",
    "CODE_PRECEDENCE",
    "COMMENTS_SCHEMA",
    "DIAGNOSTIC_CODES",
    "DOCSPEC_GENERATION",
    "DOCUMENTS_SCHEMA",
    "FORMAT",
    "FORMAT_VERSION",
    "FRAMED_SET_DOMAINS",
    "GENERATION_SCHEMA_ROLES",
    "MEMBER_MANIFEST_SCHEMA",
    "MEMBER_ROLES_BY_GENERATION",
    "PREDECESSOR_GENERATION",
    "PREDECESSOR_TABULAR_ROLES",
    "RELEASE_ID_PREFIX",
    "REPRESENTATION_MEDIA_TYPE",
    "ROOT_SCHEMA",
    "SCHEMA_FILES",
    "SCHEMA_ID_GENERATIONS",
    "SCHEMA_IDS",
    "SCHEMA_ROOT",
    "SEARCH_SEGMENTS_SCHEMA",
    "SELECTED_SOURCE_SET_DOMAIN",
    "SOURCE_CATALOG_ID_PREFIX",
    "SOURCE_DISPOSITIONS_SCHEMA",
    "SOURCE_TO_DOCUMENT_DOMAIN",
    "STRUCTURAL_NODES_SCHEMA",
    "TABULAR_MEDIA_TYPES",
    "TABULAR_ROLES",
    "TEXT_BODY_INDEX_ROLE",
    "TEXT_BODY_INDEX_ROW_DEF",
    "TEXT_BODY_KEYS",
    "TEXT_KINDS",
    "VerificationIssue",
    "VerificationResult",
    "artifact_sha256",
    "bundle_generation",
    "canonical_schema_id",
    "declared_generations",
    "derive_counts",
    "derive_coverage",
    "derive_per_kind_counts",
    "expected_document_state_digest",
    "expected_release_id",
    "framed_set_digest",
    "mapping_digest",
    "schema_id_generation",
    "stamp_root",
    "verify_corpus",
    "verify_document_release",
]
