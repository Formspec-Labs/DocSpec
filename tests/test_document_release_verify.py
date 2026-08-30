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
    TEXT_BODY_INDEX_ROLE,
    TEXT_BODY_INDEX_ROW_DEF,
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
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
    logical_content,
    safe_object_key,
    tree_digest,
)
from docspec.domain.identity import canonical_json_bytes as identity_canonical_json_bytes
from docspec.domain.storage import partition_bucket
from docspec.domain.identity import sha256_digest
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


@BOTH
def test_each_corpus_spends_exactly_one_bundle_on_every_diagnostic_code(corpus: Corpus) -> None:
    """The codes and the invalid bundles are one list, so neither can grow alone."""

    cases = corpus.cases
    invalid = [case for case in cases if case["expectedCode"] != "valid"]

    assert len(cases) == len(DIAGNOSTIC_CODES) + 1
    assert sorted(case["expectedCode"] for case in invalid) == sorted(DIAGNOSTIC_CODES)


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


def test_the_two_corpora_are_the_same_twenty_cases_under_two_generations() -> None:
    """The restamp re-minted the corpus; it did not redesign it.

    Same bundle names, same expected codes. The paths differ exactly where the
    member keys did -- `data/*.json` became `data/*.jsonl`, and one
    file-per-document member became a partition bucket -- which is what restamp
    item 11 changed and nothing more.
    """

    assert [case["name"] for case in DOCSPEC_CASES] == [case["name"] for case in CASES]
    assert [case["expectedCode"] for case in DOCSPEC_CASES] == [
        case["expectedCode"] for case in CASES
    ]
    moved = {
        case["name"]: (case["expectedPath"], docspec["expectedPath"])
        for case, docspec in zip(CASES, DOCSPEC_CASES, strict=True)
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
    fields = FRAMED_SET_DOMAINS[SOURCE_TO_DOCUMENT_DOMAIN]
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
        framed_set_digest("docspec-document-set/2", [{"documentId": None}])


def test_the_framed_domains_are_exactly_the_ones_the_decision_declares() -> None:
    assert set(FRAMED_SET_DOMAINS) == {
        "docspec-selected-source-set/1",
        "docspec-document-set/2",
        "docspec-document-version-set/2",
        "docspec-text-body-set/2",
        "docspec-attachment-set/2",
        "docspec-comment-set/2",
        "docspec-representation-set/2",
        "docspec-segment-set/2",
        "docspec-source-to-document/2",
    }


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
        assert row["textKind"] == "document-body"
    if name != "documents":
        # Re-keyed, not merely widened: the old key is gone from these members.
        assert all("documentVersionId" not in row for row in rows)


def test_the_text_body_id_of_a_document_body_equals_its_document_version_id() -> None:
    """Decision 0001's mint rule: one body per version, never a second name."""

    for document in _rows("documents"):
        assert document["textBodyId"] == document["documentVersionId"]
    bodies = {document["textBodyId"] for document in _rows("documents")}
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
    documents = _rows("documents")

    assert content["textBodySetDigest"] == framed_set_digest(
        "docspec-text-body-set/2",
        [
            {"textBodyId": document["textBodyId"], "textKind": document["textKind"]}
            for document in documents
        ],
    )
    # No attachment or comment member exists, so both are the empty set's digest
    # -- written, never omitted.
    assert content["attachmentSetDigest"] == framed_set_digest("docspec-attachment-set/2", ())
    assert content["commentSetDigest"] == framed_set_digest("docspec-comment-set/2", ())
    assert content["attachmentSetDigest"] != content["commentSetDigest"]


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
    assert content["selectedSourceSetDigest"] == framed_set_digest(
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
