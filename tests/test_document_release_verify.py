"""Exercise the DocumentRelease 2.0 verifier against BOTH sealed corpora.

Each corpus is one mutation per diagnostic code: the valid bundle must verify,
and each invalid bundle must fail with exactly the code and path it is named
for. Anything less than every bundle leaves the fixtures inert -- present,
digest-sealed, and never actually run.

There are two corpora because there are two minting generations. The
predecessor corpus is the frozen regression anchor: twenty bundles minted
before Decision 0001, which must keep verifying byte-unchanged under the
predecessor rules forever. The docspec corpus is that decision's restamp of
them -- re-homed schema ids, JSONL members, `textBodyId` keys, partitioned
text and blob members, the reshaped catalog pin, `documentStateDigest` -- and
must verify equally completely under the docspec rules. Every diagnostic code
fires on its own bundle in each. A verifier that could only do one of these
would be wrong about half of what it reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rulespec_artifacts import FramedSection, framed_section_digest
from rulespec_artifacts import canonical_json_bytes as artifact_canonical_json_bytes

from docspec.adapters.document_release_verify import (
    ALLOWED_MEMBER_ROLES,
    ATTACHMENT_DISPOSITIONS,
    CATALOG_DISPOSITIONS,
    DIAGNOSTIC_CODES,
    DOCSPEC_GENERATION,
    FRAMED_SET_DOMAINS,
    GENERATION_SCHEMA_ROLES,
    MEMBER_ROLES_BY_GENERATION,
    PREDECESSOR_GENERATION,
    RELEASE_ID_PREFIX,
    SCHEMA_FILES,
    SCHEMA_ID_GENERATIONS,
    SCHEMA_IDS,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
    TABULAR_ROLES,
    TEXT_BODY_INDEX_ROLE,
    TEXT_BODY_INDEX_ROW_DEF,
    TEXT_BODY_SET_DOMAIN,
    TEXT_KINDS,
    bundle_generation,
    canonical_schema_id,
    declared_generations,
    derive_per_kind_counts,
    expected_document_state_digest,
    expected_release_id,
    framed_set_digest,
    stamp_root,
    verify_corpus,
    verify_document_release,
)
from docspec.document_release_support import (
    LOGICAL_ROW_EXCLUSIONS,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
    logical_content,
    logical_row,
    safe_object_key,
    tree_digest,
    write_canonical_jsonl,
)
from docspec.domain.identity import canonical_json_bytes as identity_canonical_json_bytes
from docspec.domain.storage import partition_bucket
from docspec.domain.identity import sha256_digest, stable_urn
from docspec.source_catalog import selected_source_set_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "document_release_v2"
CORPUS_FILE = FIXTURE_ROOT / "corpus.json"
DOCSPEC_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "document_release_v2_docspec"
DOCSPEC_CORPUS_FILE = DOCSPEC_FIXTURE_ROOT / "corpus.json"


def _cases(corpus_file: Path) -> list[dict[str, Any]]:
    return json.loads(corpus_file.read_text(encoding="utf-8"))["cases"]


CASES: list[dict[str, Any]] = _cases(CORPUS_FILE)
INVALID_CASES = [case for case in CASES if case["expectedCode"] != "valid"]
DOCSPEC_CASES: list[dict[str, Any]] = _cases(DOCSPEC_CORPUS_FILE)


@dataclass(frozen=True)
class Corpus:
    """One sealed corpus and the minting generation it was written under."""

    generation: str
    root: Path
    corpus_file: Path

    @property
    def cases(self) -> list[dict[str, Any]]:
        return _cases(self.corpus_file)

    def __str__(self) -> str:
        return self.generation


CORPORA = (
    Corpus(PREDECESSOR_GENERATION, FIXTURE_ROOT, CORPUS_FILE),
    Corpus(DOCSPEC_GENERATION, DOCSPEC_FIXTURE_ROOT, DOCSPEC_CORPUS_FILE),
)
BOTH = pytest.mark.parametrize("corpus", CORPORA, ids=[str(item) for item in CORPORA])
EVERY_INVALID_BUNDLE = [
    (corpus, case)
    for corpus in CORPORA
    for case in corpus.cases
    if case["expectedCode"] != "valid"
]
EVERY_BUNDLE = [(corpus, case) for corpus in CORPORA for case in corpus.cases]


def _case(name: str) -> dict[str, Any]:
    return next(case for case in CASES if case["name"] == name)


@BOTH
def test_the_valid_bundle_of_each_generation_verifies_with_no_diagnostic_at_all(
    corpus: Corpus,
) -> None:
    result = verify_document_release(corpus.root / "valid")

    assert [str(issue) for issue in result.issues] == []
    assert result.valid
    assert result.code == "valid"
    assert result.release_id is not None
    assert result.release_id.startswith("urn:docspec:document-release:v2:")


@pytest.mark.parametrize(
    ("corpus", "case"),
    EVERY_INVALID_BUNDLE,
    ids=[f"{corpus}-{case['name']}" for corpus, case in EVERY_INVALID_BUNDLE],
)
def test_each_invalid_bundle_fails_with_exactly_the_diagnostic_it_is_named_for(
    corpus: Corpus, case: dict[str, Any]
) -> None:
    result = verify_document_release(corpus.root / case["bundle"])

    assert not result.valid, f"{corpus}/{case['name']} was accepted"
    assert result.code == case["expectedCode"], [str(issue) for issue in result.issues]
    assert result.path == case["expectedPath"], [str(issue) for issue in result.issues]


# The four diagnostics only a docspec-generation bundle can produce: three are
# amendment B4's, and the version binding is B2's. The sealed predecessor corpus
# was minted before any of them existed and is frozen, so it cannot spend a
# bundle on one.
DOCSPEC_ONLY_CODES = frozenset(
    {
        "invalid.version-binding",
        "invalid.comment-selection",
        "invalid.attachment-accounting",
        "invalid.retention-floor",
    }
)
# The one diagnostic no corpus fixture can reach, recorded as an absence rather
# than left to be noticed. `invalid.comment-selection` needs a comment, a comment
# is a member of the requested universe U, and a member of U carries a `selected`
# disposition row whose `documentVersionId` the sealed source-dispositions schema
# REQUIRES to be non-null ("A selected item is a document"). Neither pinned
# catalog -- the fixture's or D1's -- selects a comment, and Decision 0001
# deferred the U shape for comments deliberately. Minting one here would mean
# inventing a universe member no catalog carries, which is the class of thing
# this gate exists to catch. The rule is proved on a grown bundle instead, by
# `test_two_selection_policies_in_one_release_is_a_comment_selection_defect`.
UNMINTABLE_CODES = frozenset({"invalid.comment-selection"})
CODES_BY_GENERATION = {
    PREDECESSOR_GENERATION: frozenset(DIAGNOSTIC_CODES) - DOCSPEC_ONLY_CODES,
    DOCSPEC_GENERATION: frozenset(DIAGNOSTIC_CODES) - UNMINTABLE_CODES,
}


@BOTH
def test_each_corpus_covers_every_diagnostic_code_its_generation_can_produce(
    corpus: Corpus,
) -> None:
    """The codes and the invalid bundles are one list, so neither grows alone."""

    invalid = [case for case in corpus.cases if case["expectedCode"] != "valid"]
    covered = {case["expectedCode"] for case in invalid}

    assert covered == CODES_BY_GENERATION[corpus.generation]
    # One bundle per code, except where a second bundle proves a second rule
    # under the same code: amendment B1's guard that a `/3` digest refuses a
    # repeated key needs its own case, and duplicate identity is that code.
    spent = [case["expectedCode"] for case in invalid]
    repeated = {code for code in spent if spent.count(code) > 1}
    assert repeated <= {"invalid.duplicate-identity"}


@BOTH
def test_every_case_seals_the_whole_diagnostic_set_it_produces(corpus: Corpus) -> None:
    """Amendment B3: all codes and all paths, not just the primary pair.

    A bundle that emitted its expected diagnostic PLUS five others used to pass
    this corpus, because only the first was asserted. Sealing the whole set
    means a rule that starts firing where it did not is a test failure rather
    than a silent widening -- and the sealed lists were regenerated from
    observed behaviour, then read, rather than written from memory.
    """

    for case in corpus.cases:
        expected = case.get("expectedDiagnostics")
        if expected is None:
            # The frozen predecessor corpus predates this field and its bytes
            # are sealed; its primary code and path are asserted elsewhere.
            assert corpus.generation == PREDECESSOR_GENERATION
            continue
        result = verify_document_release(corpus.root / case["bundle"])
        observed = [{"code": issue.code, "path": issue.path} for issue in result.issues]
        assert observed == expected, case["name"]


@BOTH
def test_verifying_each_corpus_reports_every_bundle_sealed_and_as_expected(
    corpus: Corpus,
) -> None:
    rows = verify_corpus(corpus.corpus_file)

    assert len(rows) == len(corpus.cases)
    unsealed = [row["name"] for row in rows if not row["sealed"]]
    assert unsealed == [], "a fixture bundle's bytes no longer match its recorded tree digest"
    mismatched = [
        row
        for row in rows
        if row["observedCode"] != row["expectedCode"] or row["observedPath"] != row["expectedPath"]
    ]
    assert mismatched == []


@pytest.mark.parametrize(
    ("corpus", "case"),
    EVERY_BUNDLE,
    ids=[f"{corpus}-{case['name']}" for corpus, case in EVERY_BUNDLE],
)
def test_every_bundle_still_digests_to_the_tree_it_was_sealed_under(
    corpus: Corpus, case: dict[str, Any]
) -> None:
    assert tree_digest(corpus.root / case["bundle"]) == case["treeSha256"]


def test_the_docspec_corpus_is_the_sealed_one_restamped_and_then_extended() -> None:
    """The restamp re-minted the corpus; it did not redesign it.

    Every predecessor case survives, in order, under the same expected code. The
    paths differ exactly where the member keys did -- `data/*.json` became
    `data/*.jsonl`, and one file-per-document member became a partition bucket --
    which is what restamp item 11 changed and nothing more. What is ADDED is the
    four cases the amendments' new rules need, and nothing else.
    """

    docspec_names = [case["name"] for case in DOCSPEC_CASES]
    sealed_names = [case["name"] for case in CASES]
    added = [name for name in docspec_names if name not in sealed_names]

    assert [name for name in docspec_names if name in sealed_names] == sealed_names
    assert added == [
        "version-binding",
        "attachment-accounting",
        "duplicate-attachment",
        "retention-floor",
    ]
    kept = [case for case in DOCSPEC_CASES if case["name"] in sealed_names]
    assert [case["expectedCode"] for case in kept] == [
        case["expectedCode"] for case in CASES
    ]
    moved = {
        case["name"]: (case["expectedPath"], docspec["expectedPath"])
        for case, docspec in zip(CASES, kept, strict=True)
        if case["expectedPath"] != docspec["expectedPath"]
    }
    assert moved == {
        "missing-member": ("text/FR-2026-04188.txt", "text/0019"),
        "member-digest": ("data/structural-nodes.json", "data/structural-nodes.jsonl"),
        "unknown-node-kind": (
            "data/structural-nodes.json/0/nodeKind",
            "data/structural-nodes.jsonl/0/nodeKind",
        ),
        "duplicate-segment": (
            "data/search-segments.json/1/segmentId",
            "data/search-segments.jsonl/1/segmentId",
        ),
        "catalog-pin-mismatch": (
            "data/documents.json/0/capture/catalogReleaseId",
            "data/documents.jsonl/0/capture/catalogReleaseId",
        ),
        "missing-projection-reason": (
            "data/source-dispositions.json/2/reason",
            "data/source-dispositions.jsonl/2/reason",
        ),
        "expected-digest-mismatch": (
            "data/documents.json/0/capture/expectedSha256",
            "data/documents.jsonl/0/capture/expectedSha256",
        ),
        "representation-bytes-differ": (
            "data/documents.json/0/representation/sha256",
            "data/documents.jsonl/0/representation/sha256",
        ),
        "orphan-structural-parent": (
            "data/structural-nodes.json/2/structuralParentId",
            "data/structural-nodes.jsonl/2/structuralParentId",
        ),
        "segment-heading-path": (
            "data/search-segments.json/2/headingPath",
            "data/search-segments.jsonl/2/headingPath",
        ),
        "coverage-gap": (
            "data/documents.json/0/representation",
            "data/documents.jsonl/0/representation",
        ),
    }


# ─── Schema identity across the REF-048 re-homing ──────────────────────


def test_the_sealed_corpus_declares_the_predecessor_schema_identifiers() -> None:
    """The premise of the resolution: these bundles predate the re-homed ``$id``s."""

    manifest = load_strict_canonical_json(FIXTURE_ROOT / "valid" / "manifests" / "global.json")
    declared = {
        member["schemaId"] for member in manifest["members"] if member["role"] == "schema"
    }

    assert declared
    assert declared.isdisjoint(set(SCHEMA_IDS.values()))
    assert all(canonical_schema_id(value) in set(SCHEMA_IDS.values()) for value in declared)


def test_the_packaged_schema_identifiers_resolve_to_themselves() -> None:
    assert set(SCHEMA_FILES) == set(SCHEMA_IDS)
    for schema_id in SCHEMA_IDS.values():
        assert canonical_schema_id(schema_id) == schema_id
    # Every packaged spelling, plus one predecessor spelling for each role that
    # generation had. `attachments` and `comments` have no predecessor spelling
    # because they did not exist under it.
    predecessor = GENERATION_SCHEMA_ROLES[PREDECESSOR_GENERATION]
    assert len(set(SCHEMA_ID_GENERATIONS)) == len(SCHEMA_IDS) + len(predecessor)
    assert predecessor < GENERATION_SCHEMA_ROLES[DOCSPEC_GENERATION]
    assert GENERATION_SCHEMA_ROLES[DOCSPEC_GENERATION] - predecessor == {
        "attachments",
        "comments",
    }


def test_an_unregistered_schema_identifier_resolves_to_itself_and_so_fails_closed() -> None:
    assert canonical_schema_id("urn:docspec:schema:document-release:9.9") == (
        "urn:docspec:schema:document-release:9.9"
    )
    assert canonical_schema_id(None) is None


def test_each_bundle_embedded_schema_still_carries_the_id_its_descriptor_names() -> None:
    """A bundle is read as it was written: resolution never rewrites sealed bytes."""

    root = load_strict_canonical_json(FIXTURE_ROOT / "valid" / "release.json")
    manifest = load_strict_canonical_json(FIXTURE_ROOT / "valid" / "manifests" / "global.json")
    members = {
        member["schemaId"]: member
        for member in manifest["members"]
        if member["role"] == "schema"
    }

    descriptors = root["content"]["schemaSet"]["schemas"]
    assert len(descriptors) == len(GENERATION_SCHEMA_ROLES[PREDECESSOR_GENERATION])
    for descriptor in descriptors:
        member = members[descriptor["schemaId"]]
        embedded = json.loads(
            (FIXTURE_ROOT / "valid" / member["objectKey"]).read_text(encoding="utf-8")
        )
        assert embedded["$id"] == descriptor["schemaId"]
        assert member["sha256"] == descriptor["schemaSha256"]
    assert root["content"]["schemaSet"]["schemaSetId"] == (
        f"urn:spicy:schema-set:v1:{canonical_sha256(descriptors)}"
    )


# ─── The primitives the move had to supply ─────────────────────────────


def test_the_wire_contract_digest_is_the_unqualified_spelling_of_docspec_identity() -> None:
    value = {"b": [1, 2, {"a": None}], "a": "\u00e9"}

    assert canonical_json_bytes(value) == identity_canonical_json_bytes(value)
    assert sha256_digest(canonical_json_bytes(value)) == f"sha256:{canonical_sha256(value)}"


def test_the_file_digest_is_the_unqualified_spelling_of_the_files_own_bytes() -> None:
    path = FIXTURE_ROOT / "valid" / "release.json"

    assert sha256_digest(path.read_bytes()) == f"sha256:{file_sha256(path)}"


def test_strict_loading_returns_mutable_json_and_refuses_a_trailing_newline() -> None:
    """2.0 root bytes are canonical JSON in non-file form; the verifier mutates rows."""

    root = load_strict_canonical_json(FIXTURE_ROOT / "valid" / "release.json")

    assert isinstance(root, dict)
    assert isinstance(root["content"]["schemaSet"]["schemas"], list)
    with pytest.raises(ValueError):
        load_strict_canonical_json(FIXTURE_ROOT / "invalid" / "noncanonical-root" / "release.json")


def test_an_object_key_may_not_escape_traverse_or_name_a_foreign_filesystem() -> None:
    assert safe_object_key("data/documents.json")
    for refused in (
        "../escaped-search-segments.json",
        "/absolute.json",
        "data\\documents.json",
        "C:/documents.json",
        "data//documents.json",
        "./documents.json",
        "documents.json\x00",
        "",
        None,
        7,
    ):
        assert not safe_object_key(refused), refused


# ─── The docspec minting generation ────────────────────────────────────
#
# Nothing is minted in 2.0 yet, so there is no real docspec-generation bundle to
# read. The roots below are synthetic on purpose: the sealed valid root with its
# schema set re-declared under the re-homed `$id`s, which is the smallest change
# that makes a root say "I was minted under the rules Decision 0001 settled".
# That declaration is exactly what the verifier keys its generation off.


SEALED_ROOT: dict[str, Any] = load_strict_canonical_json(FIXTURE_ROOT / "valid" / "release.json")


def _redeclared(root: dict[str, Any], resolve: bool) -> dict[str, Any]:
    copied = json.loads(json.dumps(root))
    descriptors = copied["content"]["schemaSet"]["schemas"]
    for descriptor in descriptors:
        if resolve:
            descriptor["schemaId"] = canonical_schema_id(descriptor["schemaId"])
    descriptors.sort(key=lambda descriptor: descriptor["schemaId"])
    return copied


def _docspec_generation_root() -> dict[str, Any]:
    return stamp_root(_redeclared(SEALED_ROOT, resolve=True))


def _verify_root_only(bundle: Path, root: dict[str, Any]) -> Any:
    """Materialize a root-only bundle and verify it.

    Every member is missing, so the result is full of membership diagnostics.
    That is fine and is the point: identity is judged from the root alone, before
    a single member is resolved, so a root-only bundle is enough to pin which
    rules the identity check ran under.
    """

    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "release.json").write_bytes(canonical_json_bytes(root))
    return verify_document_release(bundle)


def _identity_issues(result: Any) -> list[str]:
    return [str(issue) for issue in result.issues if issue.code == "invalid.identity"]


def test_the_sealed_corpus_is_read_under_the_predecessor_minting_generation() -> None:
    assert bundle_generation(SEALED_ROOT) == PREDECESSOR_GENERATION
    assert declared_generations(SEALED_ROOT) == {PREDECESSOR_GENERATION}


def test_a_root_declaring_the_rehomed_identifiers_is_read_under_the_docspec_rules() -> None:
    root = _docspec_generation_root()

    assert declared_generations(root) == {DOCSPEC_GENERATION}
    assert bundle_generation(root) == DOCSPEC_GENERATION


def test_a_root_mixing_both_spellings_falls_back_rather_than_picking_a_winner() -> None:
    mixed = _redeclared(SEALED_ROOT, resolve=False)
    mixed["content"]["schemaSet"]["schemas"][0]["schemaId"] = SCHEMA_IDS["release-root"]

    assert declared_generations(mixed) == {PREDECESSOR_GENERATION, DOCSPEC_GENERATION}
    assert bundle_generation(mixed) == PREDECESSOR_GENERATION


def test_a_root_with_no_legible_schema_set_stays_on_the_predecessor_rules() -> None:
    for content in ({}, {"schemaSet": {}}, {"schemaSet": {"schemas": "six"}}):
        assert bundle_generation({"content": content}) == PREDECESSOR_GENERATION
    assert bundle_generation({}) == PREDECESSOR_GENERATION


def test_a_mixed_generation_schema_set_is_refused_as_a_schema_defect(tmp_path: Path) -> None:
    mixed = _redeclared(SEALED_ROOT, resolve=False)
    mixed["content"]["schemaSet"]["schemas"][0]["schemaId"] = SCHEMA_IDS["release-root"]

    result = _verify_root_only(tmp_path / "mixed", stamp_root(mixed))

    assert any(
        issue.code == "invalid.schema" and "mix minting generations" in issue.message
        for issue in result.issues
    ), [str(issue) for issue in result.issues]


def test_the_docspec_generation_mints_two_names_over_one_content() -> None:
    root = _docspec_generation_root()
    state_digest = expected_document_state_digest(root)

    assert root["documentStateDigest"] == state_digest
    assert state_digest.startswith("sha256:")
    assert root["releaseId"] == RELEASE_ID_PREFIX + state_digest.split(":", 1)[1]
    assert expected_release_id(root) == root["releaseId"]


def test_the_state_digest_is_taken_with_the_containers_canonicaliser() -> None:
    root = _docspec_generation_root()
    # The sealed root carries the singular predecessor key; give the content the
    # plural docspec-generation key so the exclusion below is exercised, not
    # vacuously true.
    root["content"]["processingPolicies"] = {"document-body/text-html": {}}
    payload = {
        "format": root["format"],
        "formatVersion": root["formatVersion"],
        "logicalContent": logical_content(root["content"]),
    }

    assert expected_document_state_digest(root) == "sha256:" + hashlib.sha256(
        artifact_canonical_json_bytes(payload)
    ).hexdigest()
    for excluded in ("globalManifest", "processingPolicies"):
        assert excluded not in payload["logicalContent"]
    for excluded in ("memberCount", "totalMemberByteSize"):
        assert excluded not in payload["logicalContent"]["counts"]
    assert payload["logicalContent"]["coverage"] == root["content"]["coverage"]


def test_the_two_generations_derive_different_names_from_one_root() -> None:
    """The rules are genuinely different, so the detection is load-bearing."""

    root = _docspec_generation_root()

    assert expected_release_id(root, generation=DOCSPEC_GENERATION) != expected_release_id(
        root, generation=PREDECESSOR_GENERATION
    )
    assert expected_release_id(
        SEALED_ROOT, generation=PREDECESSOR_GENERATION
    ) == SEALED_ROOT["releaseId"]


def test_a_docspec_generation_root_raises_no_identity_diagnostic(tmp_path: Path) -> None:
    result = _verify_root_only(tmp_path / "docspec-generation", _docspec_generation_root())

    assert _identity_issues(result) == []


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("releaseId", "release.json/releaseId"),
        ("documentStateDigest", "release.json/documentStateDigest"),
    ],
)
def test_either_docspec_generation_name_moving_alone_is_an_identity_defect(
    tmp_path: Path, field: str, path: str
) -> None:
    root = _docspec_generation_root()
    root[field] = root[field][:-1] + ("0" if root[field][-1] != "0" else "1")

    result = _verify_root_only(tmp_path / f"tampered-{field}", root)

    assert [issue.path for issue in result.issues if issue.code == "invalid.identity"] == [path]


# ─── C11b: a physical-only repack does not move the state digest ───────


def _repacked(root: dict[str, Any]) -> dict[str, Any]:
    """Change only how the bundle was written, never what it says."""

    repacked = json.loads(json.dumps(root))
    manifest = repacked["content"]["globalManifest"]
    manifest["sha256"] = "0" * 64
    manifest["byteSize"] = manifest["byteSize"] + 1
    manifest["objectKey"] = "manifests/repacked.json"
    repacked["content"]["counts"]["memberCount"] += 1
    repacked["content"]["counts"]["totalMemberByteSize"] += 1
    return repacked


def test_a_physical_only_repack_leaves_the_docspec_state_digest_where_it_was() -> None:
    root = _docspec_generation_root()
    repacked = _repacked(root)

    assert repacked["content"] != root["content"]
    assert expected_document_state_digest(repacked) == expected_document_state_digest(root)
    assert expected_release_id(repacked, generation=DOCSPEC_GENERATION) == root["releaseId"]


def test_the_same_repack_does_move_the_predecessor_generation_name() -> None:
    """The property is the docspec generation's, and only its.

    The sealed corpus digests the whole content, member manifest and packing
    counts included, so the identical repack renames it. That is why identity
    had to be made generation-aware instead of switched over.
    """

    repacked = _repacked(SEALED_ROOT)

    assert expected_release_id(
        repacked, generation=PREDECESSOR_GENERATION
    ) != SEALED_ROOT["releaseId"]
    assert bundle_generation(repacked) == PREDECESSOR_GENERATION


def test_a_content_change_still_moves_the_docspec_state_digest() -> None:
    """The exclusion is physical facts, not "anything that is not a row"."""

    root = _docspec_generation_root()
    for mutate in (
        lambda value: value["content"]["counts"].__setitem__("selectedCount", 99),
        lambda value: value["content"]["coverage"].__setitem__("representationByteTotal", 1),
        lambda value: value["content"].__setitem__("corpusId", "urn:docspec:document-corpus:other"),
    ):
        mutated = json.loads(json.dumps(root))
        mutate(mutated)
        assert expected_document_state_digest(mutated) != root["documentStateDigest"]


# ─── The framed ``/2`` set digests ─────────────────────────────────────


def test_every_declared_set_domain_streams_a_framed_members_section() -> None:
    rows = [
        {
            "sourceItemId": "federalregister.gov/2026-04188",
            "documentId": "FR-2026-04188",
            "documentVersionId": "FR-2026-04188@2026-02-20T00:00:00Z",
        },
        {
            "sourceItemId": "federalregister.gov/2026-03227",
            "documentId": "FR-2026-03227",
            "documentVersionId": "FR-2026-03227@2026-02-14T09:12:00Z",
        },
    ]
    fields = FRAMED_SET_DOMAINS[SOURCE_TO_DOCUMENT_DOMAIN].projection
    expected = framed_section_digest(
        SOURCE_TO_DOCUMENT_DOMAIN,
        (
            FramedSection(
                "members",
                2,
                sorted(
                    ({field: row[field] for field in fields} for row in rows),
                    key=lambda record: tuple(
                        record[field].encode("utf-16-be") for field in fields
                    ),
                ),
            ),
        ),
    )

    assert framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, rows) == expected
    assert framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, reversed(rows)) == expected


def test_the_source_to_document_digest_is_a_set_over_unique_keys() -> None:
    """Decision 0001's Sealed identities: unique keys, not a repeated-pair list."""

    row = {
        "sourceItemId": "federalregister.gov/2026-03227",
        "documentId": "FR-2026-03227",
        "documentVersionId": "FR-2026-03227@2026-02-14T09:12:00Z",
    }

    with pytest.raises(ValueError, match="sorted and distinct"):
        framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, [row, dict(row)])


def test_the_selected_source_digest_is_the_catalogs_own_algorithm_under_its_own_domain() -> None:
    """Derived from the pin, never recomputed under another name (section 7.5)."""

    rows = [
        {"sourceItemId": "federalregister.gov/2026-03227", "documentId": "FR-2026-03227"},
        {"sourceItemId": "federalregister.gov/2026-04188", "documentId": "FR-2026-04188"},
    ]

    assert framed_set_digest(SELECTED_SOURCE_SET_DOMAIN, rows) == selected_source_set_digest(
        len(rows), [(row["sourceItemId"], row["documentId"]) for row in rows]
    )


def test_a_framed_set_digest_refuses_an_undeclared_domain_or_an_untyped_member() -> None:
    with pytest.raises(ValueError, match="not a declared"):
        framed_set_digest("docspec-document-set/1", [{"documentId": "FR-1"}])
    with pytest.raises(ValueError, match="must be text"):
        framed_set_digest("docspec-comment-set/3", [{"commentId": None}])


def test_the_framed_domains_are_exactly_the_ones_the_decision_declares() -> None:
    """Amendment B1's `/3` table, and the one `/1` domain that stays where it is."""

    assert set(FRAMED_SET_DOMAINS) == {
        "docspec-selected-source-set/1",
        "docspec-source-disposition-set/3",
        "docspec-document-version-set/3",
        "docspec-attachment-set/3",
        "docspec-comment-set/3",
        "docspec-structural-node-set/3",
        "docspec-segment-set/3",
        "docspec-text-body-set/3",
        "docspec-source-to-document/3",
    }
    # Exactly one of the two framings, per domain, and every full-row domain
    # names a record type the exclusion table declares.
    for domain, spec in FRAMED_SET_DOMAINS.items():
        assert (spec.record_type is None) != (spec.projection is None), domain
        if spec.record_type is not None:
            assert spec.record_type in LOGICAL_ROW_EXCLUSIONS, domain


def test_a_three_domain_frames_the_full_logical_row_minus_locators_and_clocks() -> None:
    """Amendment B1, by construction: the mutation the first mint's gate missed.

    A same-length change to a body's bytes with the physical digest restamped
    left `documentVersionSetDigest` unmoved under the `/2` domains. Under `/3`
    it moves, because the row's own content digest is IN the preimage -- while a
    repack, which changes only where the bytes landed, still leaves it alone.
    """

    row = {
        "capture": {
            "acquiredAt": "2026-08-10T00:00:00Z",
            "acquisitionStartedAt": None,
            "objectKey": "blobs/0007",
            "sha256": "a" * 64,
        },
        "documentId": "FR-1",
        "documentVersionId": "FR-1@2026-01-01T00:00:00Z",
        "representation": {"objectKey": "text/0007", "sha256": "b" * 64},
        "sourceMetadata": {"sourceUrl": "https://example.gov/1", "title": "One"},
    }
    sealed = framed_set_digest("docspec-document-version-set/3", [row])

    repacked = json.loads(json.dumps(row))
    repacked["capture"]["objectKey"] = "blobs/0042"
    repacked["representation"]["objectKey"] = "text/0042"
    repacked["capture"]["acquiredAt"] = "2027-01-01T00:00:00Z"
    assert framed_set_digest("docspec-document-version-set/3", [repacked]) == sealed

    for mutation in (
        lambda value: value["representation"].__setitem__("sha256", "c" * 64),
        lambda value: value["capture"].__setitem__("sha256", "c" * 64),
        lambda value: value["sourceMetadata"].__setitem__("sourceUrl", "https://evil.gov/1"),
        lambda value: value["sourceMetadata"].__setitem__("title", "Two"),
    ):
        moved = json.loads(json.dumps(row))
        mutation(moved)
        assert framed_set_digest("docspec-document-version-set/3", [moved]) != sealed


def test_every_three_domain_refuses_a_repeated_key_rather_than_absorbing_it() -> None:
    """The peer-review guard: multiplicity is a fact, and a set digest must not eat it."""

    rows = {
        "docspec-source-disposition-set/3": {"sourceItemId": "s1"},
        "docspec-document-version-set/3": {"documentVersionId": "v1"},
        "docspec-attachment-set/3": {"attachmentId": "a1"},
        "docspec-comment-set/3": {"commentId": "c1"},
        "docspec-structural-node-set/3": {"structuralNodeId": "n1"},
        "docspec-segment-set/3": {"segmentId": "g1"},
        "docspec-text-body-set/3": {"textBodyId": "b1", "textKind": "attachment"},
        "docspec-source-to-document/3": {
            "sourceItemId": "s1",
            "documentId": "d1",
            "documentVersionId": "v1",
        },
    }
    for domain, row in rows.items():
        with pytest.raises(ValueError, match="sorted and distinct"):
            framed_set_digest(domain, [row, dict(row)])


def test_relabelling_the_sealed_corpus_does_not_make_it_a_docspec_generation_bundle(
    tmp_path: Path,
) -> None:
    """Re-declaring the `$id`s is not a restamp, and the gate says so.

    Before Decision 0001's restamp landed, swapping the schema identifiers was
    the only thing that separated the two generations, so this synthetic hybrid
    was the closest thing to a docspec-generation bundle in existence. The
    restamp reshaped the RECORDS -- JSONL members, `textBodyId` keys, the
    reshaped pin, the new digests -- so a bundle that relabels the sealed
    corpus without re-minting it now embeds schema bodies that are not the
    registered docspec generation, and is refused for exactly that.
    """

    root = _docspec_generation_root()
    bundle = tmp_path / "relabelled"
    shutil.copytree(FIXTURE_ROOT / "valid", bundle)
    (bundle / "release.json").write_bytes(canonical_json_bytes(root))

    result = verify_document_release(bundle)
    messages = [str(issue) for issue in result.issues]

    assert not result.valid
    # The records, not the identifiers: every tabular member is refused as
    # un-streamable (item 11), for declaring the predecessor's media type
    # (item 11), and every partition member for declaring no row count
    # (item 16).
    assert [item for item in messages if "JSONL record must be terminated" in item] == [
        f"invalid.schema data/{name}.json: every JSONL record must be terminated by a newline"
        for name in ("source-dispositions", "documents", "structural-nodes", "search-segments")
    ]
    assert sum("expected application/x-ndjson" in item for item in messages) == 4
    assert sum("invalid record count" in item for item in messages) == 4


def test_the_two_encoders_agree_byte_for_byte_on_this_formats_domain() -> None:
    """D2's surviving factual basis, asserted rather than cited.

    The container's canonicaliser and DocSpec's identity canonicaliser emit
    identical bytes for every value this format actually carries: the sealed
    valid root, its content object, and the logical payload the docspec
    generation digests. Where they differ (non-BMP object keys, refusal
    surfaces) is outside this format's domain.
    """
    root = SEALED_ROOT
    logical = {
        "format": root["format"],
        "formatVersion": root["formatVersion"],
        "logicalContent": logical_content(root["content"]),
    }
    for value in (root, root["content"], logical):
        assert artifact_canonical_json_bytes(value) == identity_canonical_json_bytes(value)


# ─── The docspec generation, as minted ─────────────────────────────────
#
# Everything above proves both corpora verify. What follows proves the docspec
# corpus is actually the restamp Decision 0001 specified, item by item, read off
# the minted bytes rather than off the builder that wrote them.


DOCSPEC_VALID = DOCSPEC_FIXTURE_ROOT / "valid"
DOCSPEC_ROOT: dict[str, Any] = load_strict_canonical_json(DOCSPEC_VALID / "release.json")
DOCSPEC_MANIFEST: dict[str, Any] = load_strict_canonical_json(
    DOCSPEC_VALID / "manifests" / "global.json"
)
SOURCE_CATALOG_FIXTURE = ROOT / "tests" / "fixtures" / "source_catalog_release_v1" / "valid"


def _rows(name: str) -> list[dict[str, Any]]:
    return load_strict_canonical_jsonl(DOCSPEC_VALID / "data" / f"{name}.jsonl")


def _members(role: str) -> list[dict[str, Any]]:
    return [member for member in DOCSPEC_MANIFEST["members"] if member["role"] == role]


def test_each_corpus_declares_the_generation_it_was_minted_under() -> None:
    assert bundle_generation(DOCSPEC_ROOT) == DOCSPEC_GENERATION
    assert declared_generations(DOCSPEC_ROOT) == {DOCSPEC_GENERATION}
    assert bundle_generation(SEALED_ROOT) == PREDECESSOR_GENERATION


def test_item_1_every_declared_schema_id_is_the_re_homed_docspec_spelling() -> None:
    declared = {
        descriptor["schemaId"] for descriptor in DOCSPEC_ROOT["content"]["schemaSet"]["schemas"]
    }

    assert declared == set(SCHEMA_IDS.values())
    assert all(value.startswith("urn:docspec:schema:") for value in declared)


def test_item_3_the_schema_set_is_the_eight_sealed_schemas_with_a_recomputed_id() -> None:
    """The 6 -> 8 widening, and the recomputed set id that follows it.

    Item 2's two schemas are sealed, so the set is eight and the root schema
    fixes it at eight rather than bounding it: a set that could be short is a set
    a consumer might verify half of. Every descriptor also moved to a re-homed
    `$id`, so the digest over them is a different value than the sealed corpus
    carries for its six.
    """

    schema_set = DOCSPEC_ROOT["content"]["schemaSet"]
    descriptors = schema_set["schemas"]
    root_schema = json.loads(SCHEMA_FILES["release-root"].read_text(encoding="utf-8"))
    bounds = root_schema["$defs"]["schemaSet"]["properties"]["schemas"]

    assert len(descriptors) == 8 == len(SCHEMA_IDS)
    assert (bounds["minItems"], bounds["maxItems"]) == (8, 8)
    assert {role for descriptor in descriptors for role in descriptor["roles"]} == set(
        SCHEMA_FILES
    )
    assert schema_set["schemaSetId"] == (
        f"urn:spicy:schema-set:v1:{canonical_sha256(descriptors)}"
    )
    assert schema_set["schemaSetId"] != SEALED_ROOT["content"]["schemaSet"]["schemaSetId"]
    assert len(SEALED_ROOT["content"]["schemaSet"]["schemas"]) == 6


def test_the_embedded_schemas_are_the_packaged_generation_byte_for_byte() -> None:
    """The registered body stays the contract even though the bundle carries it."""

    for role, packaged in SCHEMA_FILES.items():
        member = next(
            item
            for item in _members("schema")
            if item["schemaId"] == SCHEMA_IDS[role]
        )
        embedded = DOCSPEC_VALID / member["objectKey"]
        assert embedded.read_bytes() == packaged.read_bytes()
        assert member["sha256"] == file_sha256(packaged)


@pytest.mark.parametrize(
    "name", ["source-dispositions", "documents", "structural-nodes", "search-segments"]
)
def test_item_11_every_tabular_member_is_newline_framed_jsonl(name: str) -> None:
    member = _members(name)[0]
    raw = (DOCSPEC_VALID / member["objectKey"]).read_bytes()

    assert member["objectKey"] == f"data/{name}.jsonl"
    assert member["mediaType"] == "application/x-ndjson"
    assert raw.endswith(b"\n")
    lines = raw.split(b"\n")[:-1]
    assert len(lines) == member["recordCount"] >= 1
    # One canonical-JSON record per line, and the whole file is NOT a JSON array.
    assert [canonical_json_bytes(json.loads(line)) for line in lines] == lines
    with pytest.raises(ValueError):
        load_strict_canonical_json(DOCSPEC_VALID / member["objectKey"])


def test_item_11_text_and_blob_members_are_partition_buckets_of_the_text_body_id() -> None:
    documents = _rows("documents")

    for document in documents:
        body_id = document["textBodyId"]
        bucket = f"{partition_bucket(body_id, 64):04d}"
        assert document["capture"]["objectKey"] == f"blobs/{bucket}"
        assert document["representation"]["objectKey"] == f"text/{bucket}"
    # Not one member per document under a document-named key: the keys are
    # bucket names, and nothing in them names a document.
    for member in _members("rendition") + _members("representation"):
        assert member["objectKey"].split("/")[1].isdigit()
        assert not any(
            document["documentId"] in member["objectKey"] for document in documents
        )


def test_item_16_record_count_is_stated_per_role_not_per_has_rows() -> None:
    assert [member["recordCount"] for member in _members("schema")] == [None] * 8
    for role in ("rendition", "representation"):
        counts = [member["recordCount"] for member in _members(role)]
        assert counts and all(isinstance(count, int) and count >= 1 for count in counts)
    # The predecessor said null for exactly the members that now carry a count.
    sealed_manifest = load_strict_canonical_json(FIXTURE_ROOT / "valid" / "manifests" / "global.json")
    assert all(
        member["recordCount"] is None
        for member in sealed_manifest["members"]
        if member["role"] in {"rendition", "representation"}
    )


def test_item_13_the_manifest_role_vocabulary_gained_the_two_tabular_roles() -> None:
    """The enum widened at item 13; the gate has now caught up with it."""

    schema = json.loads(
        (SCHEMA_FILES["member-manifest"]).read_text(encoding="utf-8")
    )
    roles = schema["$defs"]["memberDescriptor"]["properties"]["role"]["enum"]

    assert "attachments" in roles
    assert "comments" in roles
    # Both roles were fail-closed while their schemas were unsealed. They are
    # sealed now, so the docspec generation judges them -- and the predecessor
    # generation, whose schemas would not govern them, still refuses them.
    assert {"attachments", "comments"} <= ALLOWED_MEMBER_ROLES
    assert {"attachments", "comments"} <= MEMBER_ROLES_BY_GENERATION[DOCSPEC_GENERATION]
    assert MEMBER_ROLES_BY_GENERATION[PREDECESSOR_GENERATION].isdisjoint(
        {"attachments", "comments", TEXT_BODY_INDEX_ROLE}
    )
    assert set(roles) == ALLOWED_MEMBER_ROLES


@pytest.mark.parametrize("name", ["documents", "structural-nodes", "search-segments"])
def test_items_4_and_5_every_row_carries_the_text_body_key_and_kind(name: str) -> None:
    rows = _rows(name)

    assert rows
    for row in rows:
        assert row["textBodyId"]
        # One text pipeline, three kinds: since amendment B4 the corpus carries
        # an attachment that IS a text body, and structure and segments hang off
        # it through the same key they hang off a document body by.
        assert row["textKind"] in TEXT_KINDS
    if name == "documents":
        assert {row["textKind"] for row in rows} == {"document-body"}
    else:
        assert {row["textKind"] for row in rows} == {"document-body", "attachment"}
    if name != "documents":
        # Re-keyed, not merely widened: the old key is gone from these members.
        assert all("documentVersionId" not in row for row in rows)


def test_the_text_body_id_of_a_document_body_equals_its_document_version_id() -> None:
    """Decision 0001's mint rule: one body per version, never a second name."""

    for document in _rows("documents"):
        assert document["textBodyId"] == document["documentVersionId"]
    bodies = {document["textBodyId"] for document in _rows("documents")}
    bodies |= {
        attachment["textBodyId"]
        for attachment in _rows("attachments")
        if attachment["textBodyId"] is not None
    }
    assert {row["textBodyId"] for row in _rows("structural-nodes")} == bodies
    assert {row["textBodyId"] for row in _rows("search-segments")} == bodies


def test_item_9_the_catalog_pin_is_the_reshaped_two_field_pin() -> None:
    pin = DOCSPEC_ROOT["content"]["sourceCatalog"]

    assert set(pin) == {"catalogId", "catalogDigest"}
    assert re.fullmatch(r"urn:docspec:source-catalog:v1:[0-9a-f]{64}", pin["catalogId"])
    # The BYTE digest of the pinned root, verified against those bytes.
    assert pin["catalogDigest"] == file_sha256(SOURCE_CATALOG_FIXTURE / "release.json")


def test_item_9_both_pin_sites_move_together() -> None:
    """The root pin and the per-document back-reference cannot disagree."""

    pinned = DOCSPEC_ROOT["content"]["sourceCatalog"]["catalogId"]

    for document in _rows("documents"):
        assert document["capture"]["catalogReleaseId"] == pinned
        assert document["sourceMetadata"]["catalogReleaseId"] == pinned


def test_item_8_the_release_id_is_derived_from_the_state_digest_by_string_form() -> None:
    state_digest = DOCSPEC_ROOT["documentStateDigest"]

    assert state_digest == expected_document_state_digest(DOCSPEC_ROOT)
    assert DOCSPEC_ROOT["releaseId"] == RELEASE_ID_PREFIX + state_digest.split(":", 1)[1]
    assert expected_release_id(DOCSPEC_ROOT) == DOCSPEC_ROOT["releaseId"]


def test_item_6_processing_policies_is_a_sorted_per_kind_array_with_digests() -> None:
    policies = DOCSPEC_ROOT["content"]["processingPolicies"]

    assert "processingPolicy" not in DOCSPEC_ROOT["content"]
    assert policies
    keys = [(policy["textKind"], policy["mediaType"]) for policy in policies]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    for policy in policies:
        assert re.fullmatch(r"[0-9a-f]{64}", policy["extractorDigest"])
        assert re.fullmatch(r"[0-9a-f]{64}", policy["segmenterDigest"])
        floor = policy["retentionFloor"]
        assert floor["population"]
        # `0 < value < 1` and `observedMinimum > value`, compared as decimals.
        value = Decimal(floor["value"])
        observed = Decimal(floor["observedMinimum"])
        assert Decimal(0) < value < Decimal(1)
        assert observed > value


def test_item_6_the_policies_sit_beside_the_identity_preimage_not_inside_it() -> None:
    """A segmenter rebuilt over unchanged text must not rename the corpus."""

    moved = json.loads(json.dumps(DOCSPEC_ROOT))
    moved["content"]["processingPolicies"][0]["segmenterDigest"] = "0" * 64

    assert moved["content"] != DOCSPEC_ROOT["content"]
    assert expected_document_state_digest(moved) == DOCSPEC_ROOT["documentStateDigest"]


def test_item_14_the_wall_clock_is_in_the_record_and_in_no_preimage() -> None:
    documents = _rows("documents")
    instants = {document["capture"]["acquiredAt"] for document in documents}
    starts = [document["capture"]["acquisitionStartedAt"] for document in documents]

    assert instants and all(instants)
    # Required and nullable, both exercised rather than merely allowed.
    assert None in starts
    assert any(start is not None for start in starts)
    # Not in the identity preimage: no acquisition instant appears anywhere in
    # the bytes `documentStateDigest` is taken over.
    payload = canonical_json_bytes(
        {
            "format": DOCSPEC_ROOT["format"],
            "formatVersion": DOCSPEC_ROOT["formatVersion"],
            "logicalContent": logical_content(DOCSPEC_ROOT["content"]),
        }
    )
    for instant in instants | {start for start in starts if start}:
        assert instant.encode("utf-8") not in payload


def test_item_15_evidence_grade_is_reserved_in_the_schema_and_unpopulated() -> None:
    schema = json.loads((SCHEMA_FILES["search-segments"]).read_text(encoding="utf-8"))
    coordinate = schema["$defs"]["evidenceCoordinate"]

    assert coordinate["properties"]["evidenceGrade"] == {
        "description": coordinate["properties"]["evidenceGrade"]["description"],
        "type": "null",
    }
    assert "evidenceGrade" not in coordinate["required"]
    assert all("evidenceGrade" not in row["evidence"] for row in _rows("search-segments"))


def test_item_10_the_coordinate_system_enum_is_closed_at_two_systems() -> None:
    schema = json.loads((SCHEMA_FILES["search-segments"]).read_text(encoding="utf-8"))
    enum = schema["$defs"]["evidenceCoordinate"]["properties"]["coordinateSystem"]["enum"]

    assert enum == ["rendition-utf8-byte", "rendition-byte"]
    assert {row["evidence"]["coordinateSystem"] for row in _rows("search-segments")} <= set(enum)


def test_item_12_the_two_prose_sites_carry_their_corrections() -> None:
    root_schema = json.loads((SCHEMA_FILES["release-root"]).read_text(encoding="utf-8"))
    segments_schema = json.loads((SCHEMA_FILES["search-segments"]).read_text(encoding="utf-8"))

    assert "DocSpec owns this schema" in root_schema["description"]
    assert "Rulespec Core owns" not in root_schema["description"]
    assert "REF-048" in root_schema["description"]
    # The single-consumer sentence the C27 disposition contradicts.
    assert "Rulespec Extrapolator" in segments_schema["description"]
    assert "SpicySearch consumes these;" not in segments_schema["description"]


def test_item_7_the_three_new_set_digests_are_declared_and_recomputable() -> None:
    content = DOCSPEC_ROOT["content"]
    attachments = _rows("attachments")

    assert content["textBodySetDigest"] == framed_set_digest(
        TEXT_BODY_SET_DOMAIN,
        [
            {"textBodyId": row["textBodyId"], "textKind": row["textKind"]}
            for row in [*_rows("documents"), *attachments]
            if row["textBodyId"] is not None
        ],
    )
    # The attachment digest streams the FULL rows (amendment B1); the comment
    # member is present and empty, so its digest is the empty set's -- written,
    # never omitted.
    assert content["attachmentSetDigest"] == framed_set_digest(
        "docspec-attachment-set/3", attachments
    )
    assert content["commentSetDigest"] == framed_set_digest("docspec-comment-set/3", ())
    assert content["attachmentSetDigest"] != content["commentSetDigest"]


def test_amendment_b1_declares_the_two_digests_that_close_the_last_uncovered_rows() -> None:
    """Every logical row in the bundle is now inside the release's name."""

    content = DOCSPEC_ROOT["content"]

    assert content["sourceDispositionSetDigest"] == framed_set_digest(
        "docspec-source-disposition-set/3", _rows("source-dispositions")
    )
    assert content["structuralNodeSetDigest"] == framed_set_digest(
        "docspec-structural-node-set/3", _rows("structural-nodes")
    )


def test_the_docspec_corpus_uses_framed_domains_where_the_sealed_corpus_used_plain() -> None:
    """The set-digest branch, on real bundles of each generation."""

    content = DOCSPEC_ROOT["content"]
    documents = _rows("documents")
    joined = [
        {
            "sourceItemId": document["sourceItemId"],
            "documentId": document["documentId"],
            "documentVersionId": document["documentVersionId"],
        }
        for document in documents
    ]

    assert content["sourceDocumentMappingDigest"] == framed_set_digest(
        SOURCE_TO_DOCUMENT_DOMAIN, joined
    )
    # Amendment B6: derived over the PINNED catalog's items, not projected from
    # the release's rows -- so it is NOT the digest of `joined`, and a consumer
    # holding the pinned bytes is the only one who can recompute it.
    catalog_items = json.loads(
        (SOURCE_CATALOG_FIXTURE / "data" / "source-items.json").read_text(encoding="utf-8")
    )
    assert content["selectedSourceSetDigest"] == framed_set_digest(
        SELECTED_SOURCE_SET_DOMAIN,
        [
            {"sourceItemId": item["sourceItemId"], "documentId": item["documentId"]}
            for item in catalog_items
        ],
    )
    assert content["selectedSourceSetDigest"] != framed_set_digest(
        SELECTED_SOURCE_SET_DOMAIN, joined
    )
    # The predecessor's list digest over pairs is a different value entirely,
    # and stays where it was.
    assert SEALED_ROOT["content"]["sourceDocumentMappingDigest"] != content[
        "sourceDocumentMappingDigest"
    ]


def test_a_physical_only_repack_of_the_real_bundle_preserves_its_state_digest() -> None:
    """C11b on a minted bundle rather than on a synthetic root."""

    repacked = _repacked(DOCSPEC_ROOT)

    assert repacked["content"] != DOCSPEC_ROOT["content"]
    assert expected_document_state_digest(repacked) == DOCSPEC_ROOT["documentStateDigest"]
    assert (
        expected_release_id(repacked, generation=DOCSPEC_GENERATION)
        == DOCSPEC_ROOT["releaseId"]
    )


def test_the_committed_docspec_corpus_is_exactly_what_the_restamper_mints(
    tmp_path: Path,
) -> None:
    """Determinism, and the seal on the tool: no hand-edit can survive here.

    Every digest, count, coverage figure, and identity in the corpus is derived
    from the fixture's own bytes, so a clean rebuild must reproduce `corpus.json`
    byte for byte. This is the check that could not pass before the restamp,
    when the tool built one generation and the committed corpus was sealed under
    another.
    """

    from tools.restamp_document_release_fixtures import build_corpus

    scratch = tmp_path / "rebuild"
    scratch.mkdir()
    rebuilt = canonical_json_bytes({"cases": build_corpus(scratch)})

    assert rebuilt == DOCSPEC_CORPUS_FILE.read_bytes()


def test_the_restamper_never_names_the_frozen_predecessor_corpus_as_its_output() -> None:
    """The anchor is only an anchor while nothing rebuilds it."""

    from tools import restamp_document_release_fixtures as restamper

    assert restamper.FIXTURE_ROOT == DOCSPEC_FIXTURE_ROOT
    assert restamper.PREDECESSOR_FIXTURE_ROOT == FIXTURE_ROOT
    assert restamper.CORPUS_FILE == DOCSPEC_CORPUS_FILE


# ─── The sealed extension: attachments, comments, and the byte index ────
#
# This corpus's synthetic content carries neither an attachment nor a comment,
# so both members are minted present and empty. Sealing a schema and never
# writing a row against it is how a contract rots, so the tests below GROW a
# real docspec bundle by one comment and one attachment -- through the
# restamper's own machinery, so every digest, count, and index slice is derived
# the way the builder derives them -- and then verify it.


COMMENT_ID = "0900006485a1b2c3"
COMMENT_TEXT = "The rule is too strict."
ATTACHMENT_TEXT = "Attached comment exhibit."
ATTACHMENT_IDENTITY = "0900006485a1b2c3-0001.pdf"


def _rendition_bytes(text: str) -> bytes:
    return b"<p>" + text.encode("utf-8") + b"</p>\n"


def _place(
    bundle: Path, index_rows: list[dict[str, Any]], family: str, body_id: str, payload: bytes
) -> tuple[str, str]:
    """Append one text body's bytes to its partition bucket and index the slice."""

    prefix = "text" if family == "text" else "blobs"
    object_key = f"{prefix}/{partition_bucket(body_id, 64):04d}"
    path = bundle / object_key
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    path.write_bytes(existing + payload)
    digest = hashlib.sha256(payload).hexdigest()
    index_rows.append(
        {
            "byteLength": len(payload),
            "family": family,
            "member": object_key,
            "sha256": digest,
            "startByte": len(existing),
            "textBodyId": body_id,
        }
    )
    return object_key, digest


def _capture(
    catalog_id: str, rendition_id: str, object_key: str, media_type: str, digest: str, size: int
) -> dict[str, Any]:
    return {
        "acquiredAt": "2026-08-10T00:00:00Z",
        "acquisitionStartedAt": None,
        "byteSize": size,
        "candidateRenditionId": rendition_id,
        "catalogReleaseId": catalog_id,
        "expectedSha256": None,
        "mediaType": media_type,
        "objectKey": object_key,
        "sha256": digest,
    }


def _representation(body_id: str, object_key: str, digest: str, size: int) -> dict[str, Any]:
    return {
        "byteSize": size,
        "encoding": "utf-8",
        "mediaType": "text/plain; charset=utf-8",
        "objectKey": object_key,
        "representationId": f"{body_id}#representation",
        "sha256": digest,
    }


def _one_body_structure(body_id: str, kind: str, size: int, rendition_sha: str) -> tuple[
    dict[str, Any], dict[str, Any]
]:
    """One paragraph node spanning the whole representation, and its segment."""

    node_id = f"{body_id}#n0"
    node = {
        "depth": 0,
        "headingText": None,
        "nodeKind": "paragraph",
        "ordinal": 0,
        "representationEnd": size,
        "representationStart": 0,
        "structuralNodeId": node_id,
        "structuralParentId": None,
        "textBodyId": body_id,
        "textKind": kind,
    }
    segment = {
        "evidence": {
            "coordinateSystem": "rendition-utf8-byte",
            "end": 3 + size,
            "renditionSha256": rendition_sha,
            "start": 3,
        },
        "headingPath": [],
        "ordinal": 0,
        "representationEnd": size,
        "representationStart": 0,
        "segmentId": f"{body_id}#s0",
        "structuralParentId": node_id,
        "textBodyId": body_id,
        "textKind": kind,
    }
    return node, segment


def _extended_bundle(tmp_path: Path, comment_id: str = COMMENT_ID) -> tuple[Path, dict[str, Any]]:
    """Grow the sealed docspec bundle by one comment and one attachment of it.

    The attachment hangs off the COMMENT rather than off a document, so the one
    ownership shape the decision allows beyond the obvious one -- a comment owns
    attachments, an attachment owns nothing -- is the shape under test.
    """

    from tools.restamp_document_release_fixtures import _restamp, _state

    bundle = tmp_path / "extended"
    shutil.copytree(DOCSPEC_VALID, bundle)
    state = _state(bundle)
    catalog_id = state["catalog"]["catalogId"]
    index_rows = list(state["textBodyIndex"])

    comment_representation = COMMENT_TEXT.encode("utf-8")
    comment_rendition = _rendition_bytes(COMMENT_TEXT)
    text_key, text_sha = _place(bundle, index_rows, "text", comment_id, comment_representation)
    blob_key, blob_sha = _place(bundle, index_rows, "blob", comment_id, comment_rendition)
    state["comments"] = [
        {
            "capture": _capture(
                catalog_id,
                f"{comment_id}#html",
                blob_key,
                "text/html",
                blob_sha,
                len(comment_rendition),
            ),
            "commentId": comment_id,
            "commentSelection": {
                "groupBy": "/data/id",
                "orderBy": "/data/attributes/modifyDate DESC NULLS LAST",
                "policyDigest": "sha256:" + "a" * 64,
                "selectedModifyDate": "2026-03-01T12:00:00Z",
                "tieDisposition": "refuse-repeated-normalized-instant",
            },
            "documentId": state["documents"][0]["documentId"],
            "excludedRanges": [],
            "representation": _representation(
                comment_id, text_key, text_sha, len(comment_representation)
            ),
            "sourceItemId": state["documents"][0]["sourceItemId"],
            "sourceIssuedVersion": state["documents"][0]["sourceIssuedVersion"],
            "textBodyId": comment_id,
            "textKind": "comment",
        }
    ]

    attachment_id = stable_urn(
        "document-release-attachment",
        {
            "attachmentIdentity": ATTACHMENT_IDENTITY,
            "ownerKind": "comment",
            "ownerTextBodyId": comment_id,
        },
        version=2,
    )
    attachment_representation = ATTACHMENT_TEXT.encode("utf-8")
    attachment_rendition = _rendition_bytes(ATTACHMENT_TEXT)
    attachment_text_key, attachment_text_sha = _place(
        bundle, index_rows, "text", attachment_id, attachment_representation
    )
    attachment_blob_key, attachment_blob_sha = _place(
        bundle, index_rows, "blob", attachment_id, attachment_rendition
    )
    # Appended, not replaced: the sealed corpus carries two attachments of its
    # own since amendment B4, and dropping them here would leave their blob
    # slices in the index with no capture naming them.
    state["attachments"] += [
        {
            "attachmentId": attachment_id,
            "attachmentIdentity": ATTACHMENT_IDENTITY,
            "attachmentTitle": "Exhibit A",
            "excludedRanges": [],
            "ownerKind": "comment",
            "ownerTextBodyId": comment_id,
            "renditions": [
                {
                    "attachmentDisposition": "text-captured",
                    "capture": _capture(
                        catalog_id,
                        f"{attachment_id}#html",
                        attachment_blob_key,
                        "text/html",
                        attachment_blob_sha,
                        len(attachment_rendition),
                    ),
                    "mediaType": "text/html",
                    "renditionOrdinal": 0,
                },
                {
                    # The enumerated bytes were never fetched, so there is no
                    # capture to carry and the loss is recorded rather than
                    # silently dropped.
                    "attachmentDisposition": "source-unavailable",
                    "capture": None,
                    "mediaType": "application/pdf",
                    "reason": "The publisher returned 404 for the enumerated attachment URL.",
                    "reasonCode": "source-not-found",
                    "renditionOrdinal": 1,
                },
            ],
            "representation": _representation(
                attachment_id,
                attachment_text_key,
                attachment_text_sha,
                len(attachment_representation),
            ),
            "textBodyId": attachment_id,
            "textKind": "attachment",
        }
    ]

    for body_id, kind, size, rendition_sha in (
        (comment_id, "comment", len(comment_representation), blob_sha),
        (attachment_id, "attachment", len(attachment_representation), attachment_blob_sha),
    ):
        node, segment = _one_body_structure(body_id, kind, size, rendition_sha)
        state["nodes"].append(node)
        state["segments"].append(segment)

    index_rows.sort(key=lambda row: (row["family"], row["textBodyId"]))
    state["textBodyIndex"] = index_rows
    # Amendment B4: a text body whose `(textKind, mediaType)` no policy governs
    # was extracted under no declared floor, and this bundle grows a kind the
    # corpus does not carry. The policy is the document body's, re-declared for
    # the comment kind, which is what per-kind policies are for.
    comment_policy = json.loads(json.dumps(state["processingPolicies"][0]))
    comment_policy["textKind"] = "comment"
    state["processingPolicies"] = sorted(
        [*state["processingPolicies"], comment_policy],
        key=lambda policy: (policy["textKind"], policy["mediaType"]),
    )
    _restamp(bundle, state)
    return bundle, state


@pytest.fixture
def extended(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    return _extended_bundle(tmp_path)


def _grown(rows: list[dict[str, Any]]) -> int:
    """Where the comment-owned attachment sits among the corpus's own.

    Since amendment B4 the sealed bundle carries two attachments of its own,
    both owned by document bodies. The grown one is the comment-owned row, and
    naming it by that rather than by a position keeps these tests reading the
    thing they are about.
    """

    return next(
        index for index, row in enumerate(rows) if row["ownerKind"] == "comment"
    )


def _restamped(bundle: Path, mutate: Any) -> Any:
    """Apply one mutation to a bundle's state, restamp, and verify."""

    from tools.restamp_document_release_fixtures import _restamp, _state

    state = _state(bundle)
    mutate(state)
    _restamp(bundle, state)
    return verify_document_release(bundle)


def test_a_bundle_carrying_a_comment_and_an_attachment_of_it_verifies_whole(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The sealed extension, exercised end to end rather than merely declared."""

    bundle, _state = extended
    result = verify_document_release(bundle)

    assert [str(issue) for issue in result.issues] == []
    assert result.valid


def test_the_grown_bundle_declares_every_kind_in_its_per_kind_counts(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Amendment A2's fields, and the identity that must hold inside each."""

    bundle, _ = extended
    counts = load_strict_canonical_json(bundle / "release.json")["content"]["counts"]
    per_kind = counts["perKind"]

    assert set(per_kind) == set(TEXT_KINDS)
    assert per_kind["comment"] == {
        "excludedByteTotal": 0,
        "representationByteTotal": len(COMMENT_TEXT),
        "segmentedByteTotal": len(COMMENT_TEXT),
        "segments": 1,
        "textBodies": 1,
    }
    # Two of the corpus's own attachments, one of which carries text, plus the
    # grown one: the kinds are counted from the rows, not from a constant.
    attachments = load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
    with_text = [row for row in attachments if row["textBodyId"] is not None]
    assert per_kind["attachment"]["textBodies"] == len(with_text)
    assert per_kind["attachment"]["representationByteTotal"] == sum(
        row["representation"]["byteSize"] for row in with_text
    )
    assert per_kind["attachment"]["representationByteTotal"] > len(ATTACHMENT_TEXT)
    assert per_kind["document-body"]["textBodies"] == 2
    for kind, totals in per_kind.items():
        assert (
            totals["segmentedByteTotal"] + totals["excludedByteTotal"]
            == totals["representationByteTotal"]
        ), kind
    # `coverage` stays aggregate, and is the sum over the kinds.
    coverage = load_strict_canonical_json(bundle / "release.json")["content"]["coverage"]
    for field in ("representationByteTotal", "segmentedByteTotal", "excludedByteTotal"):
        assert coverage[field] == sum(totals[field] for totals in per_kind.values())


def test_a_per_kind_total_that_does_not_balance_is_a_coverage_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The identity is checked per kind, not only where the aggregate balances.

    The mutation moves bytes BETWEEN kinds so the aggregate still balances,
    which is exactly the hole a single aggregate figure would hide.
    """

    bundle, _ = extended
    root = load_strict_canonical_json(bundle / "release.json")
    per_kind = root["content"]["counts"]["perKind"]
    per_kind["comment"]["segmentedByteTotal"] -= 5
    per_kind["attachment"]["segmentedByteTotal"] += 5
    (bundle / "release.json").write_bytes(canonical_json_bytes(stamp_root(root)))

    result = verify_document_release(bundle)
    coverage_paths = [issue.path for issue in result.issues if issue.code == "invalid.coverage"]

    assert "release.json/content/counts/perKind/comment" in coverage_paths
    assert "release.json/content/counts/perKind/attachment" in coverage_paths


def test_the_attachment_id_is_minted_over_the_ordinal_free_preimage(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Amendment A1: the id names the attachment, the ordinal names its renditions."""

    bundle, _ = extended
    rows = load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
    attachment = rows[_grown(rows)]

    assert attachment["attachmentId"] == stable_urn(
        "document-release-attachment",
        {
            "attachmentIdentity": attachment["attachmentIdentity"],
            "ownerKind": attachment["ownerKind"],
            "ownerTextBodyId": attachment["ownerTextBodyId"],
        },
        version=2,
    )
    # Two renditions under one id: an id carrying the ordinal could not do this.
    assert [row["renditionOrdinal"] for row in attachment["renditions"]] == [0, 1]
    assert attachment["textBodyId"] == attachment["attachmentId"]
    schema = json.loads(SCHEMA_FILES["attachments"].read_text(encoding="utf-8"))
    assert "renditionOrdinal" not in schema["properties"]
    assert (
        "renditionOrdinal"
        in schema["$defs"]["rendition"]["properties"]
    )


def test_an_attachment_id_minted_over_another_preimage_is_an_identity_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["attachments"][0]["attachmentIdentity"] = "0900006485a1b2c3-0002.pdf"

    result = _restamped(bundle, mutate)

    assert result.code == "invalid.identity"
    assert result.path == "data/attachments.jsonl/0/attachmentId"


def test_an_attachment_owned_by_nothing_in_this_release_is_a_join_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        attachment = state["attachments"][0]
        attachment["ownerTextBodyId"] = "0900006485deadbeef"
        attachment["attachmentId"] = stable_urn(
            "document-release-attachment",
            {
                "attachmentIdentity": attachment["attachmentIdentity"],
                "ownerKind": attachment["ownerKind"],
                "ownerTextBodyId": attachment["ownerTextBodyId"],
            },
            version=2,
        )
        attachment["textBodyId"] = attachment["attachmentId"]

    result = _restamped(bundle, mutate)
    joins = [issue for issue in result.issues if issue.code == "invalid.join"]

    assert [issue.path for issue in joins] == ["data/attachments.jsonl/0/ownerTextBodyId"]


def test_an_attachment_that_names_its_owner_by_the_wrong_kind_is_a_join_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The owner exists; the row says it is a document body and it is a comment."""

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        attachment = state["attachments"][_grown(state["attachments"])]
        attachment["ownerKind"] = "document-body"
        attachment["attachmentId"] = stable_urn(
            "document-release-attachment",
            {
                "attachmentIdentity": attachment["attachmentIdentity"],
                "ownerKind": attachment["ownerKind"],
                "ownerTextBodyId": attachment["ownerTextBodyId"],
            },
            version=2,
        )
        attachment["textBodyId"] = attachment["attachmentId"]

    result = _restamped(bundle, mutate)
    joins = [issue for issue in result.issues if issue.code == "invalid.join"]

    assert [issue.path for issue in joins] == ["data/attachments.jsonl/2/ownerKind"]


def test_a_failed_rendition_without_a_reason_is_an_accounting_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Loss stays visible: a row may say it could not capture, never say nothing.

    Amendment B4 moved this off `invalid.disposition`. An attachment disposition
    is not a catalog disposition -- Decision 0001 chose the four tokens
    specifically so a reader could not join the two vocabularies -- so the
    diagnostic must not join them either.
    """

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        del state["attachments"][_grown(state["attachments"])]["renditions"][1]["reason"]

    result = _restamped(bundle, mutate)

    assert result.code == "invalid.schema"
    assert any(
        issue.code == "invalid.attachment-accounting"
        and issue.path == "data/attachments.jsonl/2/renditions/1/reason"
        for issue in result.issues
    )


def test_the_four_attachment_disposition_tokens_are_closed_and_not_the_catalogs(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """An attachment disposition is not a catalog disposition, and cannot be read as one."""

    schema = json.loads(SCHEMA_FILES["attachments"].read_text(encoding="utf-8"))
    tokens = schema["$defs"]["rendition"]["properties"]["attachmentDisposition"]["enum"]

    assert tokens == list(ATTACHMENT_DISPOSITIONS)
    assert set(tokens).isdisjoint(CATALOG_DISPOSITIONS)
    bundle, _ = extended
    observed = {
        rendition["attachmentDisposition"]
        for row in load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
        for rendition in row["renditions"]
    }
    assert observed <= set(tokens)


def test_the_attachment_reason_code_is_a_bounded_kebab_case_string_not_an_enum() -> None:
    """Amendment A3: sealed as a bound now, closed as an enum at the real mint."""

    schema = json.loads(SCHEMA_FILES["attachments"].read_text(encoding="utf-8"))
    reason_code = schema["$defs"]["reasonCode"]

    assert reason_code["type"] == "string"
    assert "enum" not in reason_code
    assert reason_code["maxLength"] == 64
    assert re.fullmatch(reason_code["pattern"], "source-not-found")
    for refused in ("Source-Not-Found", "source_not_found", "source not found", "-source", "x" * 65):
        assert not (
            re.fullmatch(reason_code["pattern"], refused) and len(refused) <= 64
        ), refused


def test_a_comment_row_projects_the_sealed_selection_policy_verbatim(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """DocSpec inherits the refusal, so it must not be able to state another policy."""

    bundle, _ = extended
    comment = load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl")[0]
    schema = json.loads(SCHEMA_FILES["comments"].read_text(encoding="utf-8"))
    policy = schema["$defs"]["commentSelection"]["properties"]

    assert comment["commentId"] == comment["textBodyId"]
    assert policy["groupBy"]["const"] == "/data/id"
    assert policy["orderBy"]["const"] == "/data/attributes/modifyDate DESC NULLS LAST"
    assert policy["tieDisposition"]["const"] == "refuse-repeated-normalized-instant"
    assert comment["commentSelection"]["tieDisposition"] == (
        "refuse-repeated-normalized-instant"
    )


def test_a_repeated_comment_id_is_a_duplicate_identity_not_a_tie_to_resolve(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Handed two observations of one comment id, the build fails rather than picks."""

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["comments"].append(json.loads(json.dumps(state["comments"][0])))

    result = _restamped(bundle, mutate)

    assert result.code == "invalid.duplicate-identity"
    assert result.path == "data/comments.jsonl/1/commentId"


def test_a_comment_filed_against_no_document_in_this_release_is_a_join_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["comments"][0]["documentId"] = "FR-2026-99999"

    result = _restamped(bundle, mutate)
    joins = [issue for issue in result.issues if issue.code == "invalid.join"]

    assert [issue.path for issue in joins] == ["data/comments.jsonl/0/documentId"]


def test_the_set_digests_now_stream_the_rows_rather_than_the_empty_set(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The empty-set digests the corpus carries are a measurement, not a constant."""

    bundle, _ = extended
    content = load_strict_canonical_json(bundle / "release.json")["content"]
    attachments = load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
    comments = load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl")

    assert content["attachmentSetDigest"] == framed_set_digest(
        "docspec-attachment-set/3", attachments
    )
    assert content["commentSetDigest"] == framed_set_digest(
        "docspec-comment-set/3", comments
    )
    assert content["attachmentSetDigest"] != DOCSPEC_ROOT["content"]["attachmentSetDigest"]
    assert content["commentSetDigest"] != DOCSPEC_ROOT["content"]["commentSetDigest"]
    # And the text-body set spans all three kinds now.
    assert content["textBodySetDigest"] != DOCSPEC_ROOT["content"]["textBodySetDigest"]


# ─── Amendment A4: the text-body index over partitioned members ────────


TEXT_BODY_INDEX_KEY = "manifests/text-body-index.jsonl"


def _index(bundle: Path) -> list[dict[str, Any]]:
    return load_strict_canonical_jsonl(bundle / TEXT_BODY_INDEX_KEY)


def _reseal(bundle: Path) -> None:
    """Re-derive every member digest, the manifest reference, and the identity.

    Everything a hand-edit of a member's bytes invalidates -- and nothing a
    hand-edit of that member's CONTENT should silently repair. The restamper
    re-derives the index slice digests from the bytes, which is right for a
    builder and wrong for a test that needs a slice digest to disagree with
    them, so the resealing here stops short of the row values themselves.
    """

    manifest_key = "manifests/global.json"
    manifest = load_strict_canonical_json(bundle / manifest_key)
    for member in manifest["members"]:
        path = bundle / member["objectKey"]
        member["byteSize"] = path.stat().st_size
        member["sha256"] = file_sha256(path)
    manifest["counts"]["totalByteSize"] = sum(
        member["byteSize"] for member in manifest["members"]
    )
    (bundle / manifest_key).write_bytes(canonical_json_bytes(manifest))
    root = load_strict_canonical_json(bundle / "release.json")
    root["content"]["globalManifest"]["byteSize"] = (bundle / manifest_key).stat().st_size
    root["content"]["globalManifest"]["sha256"] = file_sha256(bundle / manifest_key)
    root["content"]["counts"]["totalMemberByteSize"] = manifest["counts"]["totalByteSize"]
    (bundle / "release.json").write_bytes(canonical_json_bytes(stamp_root(root)))


def test_the_index_covers_every_text_body_and_tiles_every_partition_member() -> None:
    """The sealed corpus carries the index for its single-body buckets too.

    An accounting that only appears at scale is an accounting nobody tested, so
    the index is minted for every body in every bucket rather than only where a
    bucket is shared.
    """

    rows = _index(DOCSPEC_VALID)
    bodies = [row["textBodyId"] for row in _rows("documents")]
    # An attachment that carries text is a text body like any other, and its
    # slices are indexed like any other's. One that carries none -- every
    # rendition excluded or unavailable -- has no slice to index.
    bodies += [
        row["textBodyId"] for row in _rows("attachments") if row["textBodyId"] is not None
    ]

    assert {(row["family"], row["textBodyId"]) for row in rows} == {
        (family, body) for family in ("text", "blob") for body in bodies
    }
    by_member: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_member.setdefault(row["member"], []).append(row)
        assert row["sha256"] == hashlib.sha256(
            (DOCSPEC_VALID / row["member"]).read_bytes()[
                row["startByte"] : row["startByte"] + row["byteLength"]
            ]
        ).hexdigest()
    for member, slices in by_member.items():
        cursor = 0
        for row in sorted(slices, key=lambda item: item["startByte"]):
            assert row["startByte"] == cursor
            cursor += row["byteLength"]
        assert cursor == (DOCSPEC_VALID / member).stat().st_size


def test_the_index_member_is_governed_by_the_member_manifest_schema_not_a_ninth() -> None:
    """The schema set stays at eight; the row shape rides in the manifest schema."""

    member = _members(TEXT_BODY_INDEX_ROLE)[0]
    schema = json.loads(SCHEMA_FILES["member-manifest"].read_text(encoding="utf-8"))

    assert member["objectKey"] == TEXT_BODY_INDEX_KEY
    assert member["schemaId"] == SCHEMA_IDS["member-manifest"]
    assert member["mediaType"] == "application/x-ndjson"
    assert member["recordCount"] == len(_index(DOCSPEC_VALID))
    assert TEXT_BODY_INDEX_ROW_DEF in schema["$defs"]
    assert len(DOCSPEC_ROOT["content"]["schemaSet"]["schemas"]) == 8


def _colliding_id(body_id: str) -> str:
    """A comment id that buckets where ``body_id`` already did."""

    target = partition_bucket(body_id, 64)
    for candidate in range(100_000):
        value = f"0900006485{candidate:06d}"
        if partition_bucket(value, 64) == target:
            return value
    raise AssertionError("no colliding identifier found")


def test_a_bucket_shared_by_two_text_bodies_verifies_through_the_index(
    tmp_path: Path,
) -> None:
    """The refusal A4 lifts, demonstrated on a bucket that actually is shared.

    Before the amendment the builder refused to mint this at all: nothing could
    recover one body's bytes from a bucket holding two. Now the index says where
    each body's slice is, and the whole bundle verifies.
    """

    document_body = _rows("documents")[0]["textBodyId"]
    shared_id = _colliding_id(document_body)
    bundle, _ = _extended_bundle(tmp_path, comment_id=shared_id)

    bucket = f"text/{partition_bucket(document_body, 64):04d}"
    sharing = [row for row in _index(bundle) if row["member"] == bucket]
    result = verify_document_release(bundle)

    assert sorted(row["textBodyId"] for row in sharing) == sorted(
        {document_body, shared_id}
    )
    # Two slices, disjoint, tiling the member they share.
    cursor = 0
    for row in sorted(sharing, key=lambda item: item["startByte"]):
        assert row["startByte"] == cursor
        cursor += row["byteLength"]
    assert cursor == (bundle / bucket).stat().st_size
    assert [str(issue) for issue in result.issues] == []


def test_an_indexed_slice_whose_digest_disagrees_with_its_bytes_is_a_member_digest_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended
    rows = _index(bundle)
    position = next(
        index for index, row in enumerate(rows) if row["textBodyId"] == COMMENT_ID
    )
    rows[position]["sha256"] = "0" * 64
    (bundle / TEXT_BODY_INDEX_KEY).write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    )
    _reseal(bundle)

    result = verify_document_release(bundle)

    assert result.code == "invalid.member-digest"
    assert result.path == f"{TEXT_BODY_INDEX_KEY}/{position}/sha256"


def test_an_indexed_slice_reaching_past_its_member_is_a_member_digest_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended
    rows = _index(bundle)
    position = next(
        index for index, row in enumerate(rows) if row["textBodyId"] == COMMENT_ID
    )
    rows[position]["byteLength"] += 4096
    (bundle / TEXT_BODY_INDEX_KEY).write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    )
    _reseal(bundle)

    result = verify_document_release(bundle)

    assert result.code == "invalid.member-digest"
    assert result.path == f"{TEXT_BODY_INDEX_KEY}/{position}/byteLength"


def test_the_per_kind_counts_are_exactly_what_the_members_recompute_to(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The root's breakdown is a recomputation, never an assertion."""

    bundle, _ = extended
    root = load_strict_canonical_json(bundle / "release.json")

    assert root["content"]["counts"]["perKind"] == derive_per_kind_counts(
        load_strict_canonical_jsonl(bundle / "data" / "documents.jsonl"),
        load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl"),
        load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl"),
        load_strict_canonical_jsonl(bundle / "data" / "search-segments.jsonl"),
        key="textBodyId",
    )


# ─── Amendment B2/B3/B4: the rules the first mint's gate did not have ──


def test_two_selection_policies_in_one_release_is_a_comment_selection_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Decision 0001's inherited refusal, given the diagnostic it named.

    DocSpec does not select comments; the sealed upstream policy does, and every
    row projects that ONE policy verbatim. Two policy digests in one release is
    the release claiming a selection nobody sealed -- and no schema can see it,
    because a schema reads one row at a time.

    This rule has no invalid-corpus fixture and the reason is recorded at
    `UNMINTABLE_CODES`: a comment is a member of U, and the sealed
    source-dispositions schema requires a selected U member to be a document.
    """

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        second = json.loads(json.dumps(state["comments"][0]))
        second["commentId"] = "0900006485000001"
        second["textBodyId"] = second["commentId"]
        second["commentSelection"]["policyDigest"] = "sha256:" + "b" * 64
        # A second comment with no text of its own would break the coverage
        # identity, so it shares the first one's bytes: what is under test is
        # the policy it projects, not the body it carries.
        state["comments"].append(second)
        for row in (*state["nodes"], *state["segments"]):
            if row["textKind"] != "comment":
                continue
            twin = json.loads(json.dumps(row))
            twin["textBodyId"] = second["commentId"]
            for key in ("structuralNodeId", "segmentId"):
                if key in twin:
                    twin[key] = twin[key].replace(row["textBodyId"], second["commentId"])
            if twin.get("structuralParentId"):
                twin["structuralParentId"] = twin["structuralParentId"].replace(
                    row["textBodyId"], second["commentId"]
                )
            (state["nodes"] if "structuralNodeId" in twin else state["segments"]).append(twin)

    result = _restamped(bundle, mutate)
    selection = [issue for issue in result.issues if issue.code == "invalid.comment-selection"]

    assert [issue.path for issue in selection] == [
        "data/comments.jsonl/1/commentSelection/policyDigest"
    ]


def test_a_text_body_no_processing_policy_governs_is_a_retention_floor_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The checkable half of "an undeclared floor fails closed" (amendment B4)."""

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["processingPolicies"] = [
            policy for policy in state["processingPolicies"] if policy["textKind"] != "comment"
        ]

    result = _restamped(bundle, mutate)
    floors = [issue for issue in result.issues if issue.code == "invalid.retention-floor"]

    assert [issue.path for issue in floors] == ["data/comments.jsonl/0/capture/mediaType"]
    assert "no declared floor" in floors[0].message


def test_a_negative_index_offset_is_refused_rather_than_crashing_the_gate(
    tmp_path: Path,
) -> None:
    """Amendment B3, by construction: the crash the first mint's gate regressed to.

    `_read_slice` seeks to a caller-supplied offset. A negative `startByte` used
    to reach `seek` and raise an uncaught `OSError`, because the guard above it
    checked only the overflow end of the range -- so an untrusted bundle could
    take the gate down instead of being refused by it.
    """

    bundle = tmp_path / "negative-offset"
    shutil.copytree(DOCSPEC_VALID, bundle)
    rows = load_strict_canonical_jsonl(bundle / "manifests" / "text-body-index.jsonl")
    rows[0]["startByte"] = -8
    write_canonical_jsonl(bundle / "manifests" / "text-body-index.jsonl", rows)
    _reseal(bundle)

    result = verify_document_release(bundle)

    assert not result.valid
    assert any(
        issue.code == "invalid.member-digest"
        and issue.path == "manifests/text-body-index.jsonl/0/startByte"
        for issue in result.issues
    ), [str(issue) for issue in result.issues]


def test_read_slice_refuses_a_negative_range_before_it_seeks(tmp_path: Path) -> None:
    from docspec.adapters.document_release_verify import _read_slice

    path = tmp_path / "member"
    path.write_bytes(b"0123456789")

    assert _read_slice(path, 2, 3) == b"234"
    for start, length in ((-1, 3), (2, -3)):
        with pytest.raises(ValueError, match="non-negative"):
            _read_slice(path, start, length)


def test_the_gate_always_produces_a_verdict_even_when_a_rule_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that can be crashed is a gate that can be skipped (amendment B3)."""

    import docspec.adapters.document_release_verify as module

    def explode(_bundle: Path) -> Any:
        raise RuntimeError("a rule reached an unreachable state")

    monkeypatch.setattr(module, "_verify_document_release", explode)
    result = module.verify_document_release(tmp_path)

    assert not result.valid
    assert result.code == "invalid.root-syntax"
    assert result.path == "release.json"
    assert "RuntimeError" in result.issues[0].message


def test_the_logical_row_exclusion_table_is_exactly_the_amendments_two_kinds() -> None:
    """Physical locators and process-provenance facts, and nothing else."""

    assert set(LOGICAL_ROW_EXCLUSIONS) == set(TABULAR_ROLES)
    for record_type, paths in LOGICAL_ROW_EXCLUSIONS.items():
        for path in paths:
            leaf = path.rsplit(".", 1)[-1]
            assert leaf in {"objectKey", "acquiredAt", "acquisitionStartedAt"}, (
                record_type,
                path,
            )
    # The three record types with no locator and no clock keep their whole row.
    for record_type in ("source-dispositions", "structural-nodes", "search-segments"):
        assert LOGICAL_ROW_EXCLUSIONS[record_type] == ()
        row = {"a": 1, "b": {"c": 2}}
        assert logical_row(record_type, row) == row
