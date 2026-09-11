"""Portable DocumentRelease format generations and shared identity rules.

Ported under REF-048 from ``rulespec_conformance/document_release.py`` at
c584a1d9fcb89fb8c4253b5bb6879741b0e24c1c. Decision 0001 and its amendments
own the later DocSpec-generation changes; the predecessor fixtures stay sealed.

Version 2.0 was chosen when the application release state still used 1.1,
so the portable bundle would not look like an older compatible root. The
application state now also advertises 2.0, but remains a different shape:
active layers, blob roots, and store receipts versus self-contained document
members. Use each representation's own reader (see docs/architecture.md).
The portable identity namespace remains ``urn:docspec:document-release:v2:``.

A bundle's schema IDs select its minting generation. The predecessor uses
plain sorted-set digests and its own embedded schemas. The DocSpec generation
uses logical content, the artifact canonicalizer, and framed set digests
(including the /3 domains amended after the first mint). Registry aliases
identify roles; they never rewrite an embedded schema or its sealed digest.
Mixed generations are refused, and DocSpec schemas must match the packaged
bytes. Predecessor schemas remain readable as written.

Generation also controls JSON arrays versus JSONL, documentVersionId versus
textBodyId, and null versus counted opaque members. Read all those rules from
the same generation so parsing and validation cannot silently diverge.
Canonical bytes, safe paths, and tree digests come from
``docspec.document_release_support``. Member reading and semantic validation
live beside this module; builders consume these identity rules directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rulespec_artifacts import FramedSection, framed_section_digest
from rulespec_artifacts import canonical_json_bytes as artifact_canonical_json_bytes

from docspec.document_release_support import (
    canonical_sha256,
    logical_content,
    logical_row,
    packaged_schema_root,
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


# ─── The closed reason-code vocabularies ───────────────────────────────
#
# Decision 0001 amendment B7 closed these two lists "in this decision, not in the
# schemas": the schemas keep their dotted and kebab-case patterns as the OUTER
# bound, which is what makes a producer's new code a decision to record in that
# amendment rather than a schema migration. Amendment C4 supplies the inner bound
# -- until it, the lists were prose nothing read, and a code outside them
# (`unmapped-rendition-format`) had already been written and never noticed.
#
# Transcribed from the amendment rather than derived, because that is where they
# are decided; a code here that is not there, or there and not here, is a bug in
# one of the two and the test that compares them says which.

# The refusals THIS producer mints, on `data/source-dispositions.jsonl` (B7).
_MINTED_SOURCE_DISPOSITION_REASON_CODES: frozenset[str] = frozenset(
    {
        "catalog.state-deleted",
        "catalog.state-excluded",
        "selection.no-markup-rendition",
        "capture.no-preserved-copy",
        "capture.preserved-copy-unverifiable",
        "capture.expected-digest-differs",
        "extraction.no-extractor",
        "extraction.unparseable-source",
        "extraction.no-visible-text",
        "extraction.retention-floor-undeclared",
        "extraction.below-retention-floor",
        "extraction.retention-unmeasurable",
        "segmentation.refused",
        "segmentation.no-searchable-segment",
        "segmentation.segment-over-declared-bound",
        "structure.heading-path-disagrees",
        "metadata.incomplete",
    }
)


# The codes a PINNED CATALOG supplies and a producer projects verbatim (C4.3).
# The 10k builder mints its own and emits none of these; the sealed conformance
# corpus projects its catalog's four, and a closed list that cannot spell the
# only sealed corpus in existence is a list the gate cannot turn on.
_PROJECTED_SOURCE_DISPOSITION_REASON_CODES: frozenset[str] = frozenset(
    {
        "policy.document-type-out-of-scope",
        "source.withdrawn-after-publication",
        "source.rendition-forbidden",
        "source.metadata-unparsable",
    }
)


SOURCE_DISPOSITION_REASON_CODES: frozenset[str] = (
    _MINTED_SOURCE_DISPOSITION_REASON_CODES | _PROJECTED_SOURCE_DISPOSITION_REASON_CODES
)


# The two codes that mean THE CATALOG did not select this item, as opposed to
# this producer refusing an item the catalog did select. Amendment C3 reads the
# catalog-selected member set off exactly these, which is sound only because the
# vocabulary above is enforced: an invented code could otherwise move an item in
# or out of that set with nothing to say so.
CATALOG_STATE_REASON_CODES: frozenset[str] = frozenset(
    {"catalog.state-deleted", "catalog.state-excluded"}
)


# The kebab-case codes on the sub-rows of `data/attachments.jsonl` (B7, C4.1).
ATTACHMENT_RENDITION_REASON_CODES: frozenset[str] = frozenset(
    {
        "owner-body-rendition",
        "no-preserved-copy",
        "unmapped-rendition-format",
    }
)


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


# ─── Framed set digests: the docspec generation's ``/3`` domains ───────

# Decision 0001, amendment B1. The `/2` domains projected every row onto its id
# fields, so a same-length mutation of a body's bytes with the physical digests
# restamped left `documentStateDigest` unmoved: identity did not name content.
# The `/3` domains frame each record's FULL LOGICAL ROW -- the canonical row
# minus `LOGICAL_ROW_EXCLUSIONS`, which drops exactly the physical locators and
# the acquisition wall clock -- so a repack still preserves the name and any
# logical fact moves it.
#
# Two domains stay PROJECTIONS, because the fact each names is a projection and
# its rows are covered whole by another domain: the cross-kind text-body census,
# and the source-to-document mapping.
SELECTED_SOURCE_SET_DOMAIN = "docspec-selected-source-set/1"


SOURCE_TO_DOCUMENT_DOMAIN = "docspec-source-to-document/3"


TEXT_BODY_SET_DOMAIN = "docspec-text-body-set/3"


@dataclass(frozen=True, slots=True)
class SetDomain:
    """One declared set domain: what it keys on, and what it frames.

    Exactly one of ``record_type`` and ``projection`` is set. ``record_type``
    frames the member's full logical row under that type's exclusion set;
    ``projection`` frames exactly the named text fields, in that order.
    """

    key: str
    record_type: str | None = None
    projection: tuple[str, ...] | None = None


FRAMED_SET_DOMAINS: dict[str, SetDomain] = {
    # The catalog's own domain, at `/1`, because the digest is the same fact
    # under the same name: the release DERIVES it over the pinned catalog's
    # items rather than projecting one the D1 snapshot does not carry, which
    # is why the name may stay (spec section 7.5, Decision 0001 restamp item 9,
    # amendment B6). It is a projection of catalog items, not of release rows.
    SELECTED_SOURCE_SET_DOMAIN: SetDomain(
        key="sourceItemId", projection=("sourceItemId", "documentId")
    ),
    "docspec-source-disposition-set/3": SetDomain(
        key="sourceItemId", record_type="source-dispositions"
    ),
    "docspec-document-version-set/3": SetDomain(
        key="documentVersionId", record_type="documents"
    ),
    "docspec-attachment-set/3": SetDomain(key="attachmentId", record_type="attachments"),
    "docspec-comment-set/3": SetDomain(key="commentId", record_type="comments"),
    "docspec-structural-node-set/3": SetDomain(
        key="structuralNodeId", record_type="structural-nodes"
    ),
    "docspec-segment-set/3": SetDomain(key="segmentId", record_type="search-segments"),
    TEXT_BODY_SET_DOMAIN: SetDomain(
        key="textBodyId", projection=("textBodyId", "textKind")
    ),
    SOURCE_TO_DOCUMENT_DOMAIN: SetDomain(
        key="sourceItemId",
        projection=("sourceItemId", "documentId", "documentVersionId"),
    ),
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
    `catalog_artifact.digests._framed_digest` wraps -- so nothing here
    re-derives a digest algorithm. What is written out is the discipline around
    it that `selected_source_set_digest`
    in `catalog_artifact.digests` states: a declared count, a
    UTF-16-ordered key, and a refusal on a repeated key.

    Callers hand over the member rows exactly as the bundle carries them. A
    full-row domain projects each through `logical_row` here rather than at the
    call site, so producer and gate cannot disagree about which fields a `/3`
    digest covers (amendment B1).
    """

    spec = FRAMED_SET_DOMAINS.get(domain)
    if spec is None:
        raise ValueError(f"{domain!r} is not a declared DocumentRelease 2.0 set domain")
    keyed: list[tuple[bytes, Any]] = []
    for row in rows:
        key_value = row.get(spec.key)
        if not isinstance(key_value, str):
            raise ValueError(f"{domain} member field {spec.key!r} must be text")
        if spec.projection is not None:
            record: Any = {}
            for field in spec.projection:
                value = row.get(field)
                if not isinstance(value, str):
                    raise ValueError(f"{domain} member field {field!r} must be text")
                record[field] = value
        else:
            record = logical_row(str(spec.record_type), row)
        keyed.append((_utf16_key(key_value), record))
    keyed.sort(key=lambda entry: entry[0])

    def stream() -> Iterable[Any]:
        previous: bytes | None = None
        for current, record in keyed:
            if previous is not None and current <= previous:
                raise ValueError(f"{domain} members must be sorted and distinct")
            previous = current
            yield record

    try:
        return framed_section_digest(domain, (FramedSection("members", len(keyed), stream()),))
    except (TypeError, ValueError) as error:
        raise ValueError(f"cannot compute {domain}: {error}") from error
