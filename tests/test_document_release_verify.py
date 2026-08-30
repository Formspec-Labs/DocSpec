"""Exercise the ported DocumentRelease 2.0 verifier against the whole sealed corpus.

The corpus is one mutation per diagnostic code: the valid bundle must verify,
and each invalid bundle must fail with exactly the code and path it is named
for. Anything less than every bundle leaves the fixtures inert -- present,
digest-sealed, and never actually run.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from rulespec_artifacts import FramedSection, framed_section_digest
from rulespec_artifacts import canonical_json_bytes as artifact_canonical_json_bytes

from docspec.adapters.document_release_verify import (
    DIAGNOSTIC_CODES,
    DOCSPEC_GENERATION,
    FRAMED_SET_DOMAINS,
    PREDECESSOR_GENERATION,
    RELEASE_ID_PREFIX,
    SCHEMA_FILES,
    SCHEMA_ID_GENERATIONS,
    SCHEMA_IDS,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
    bundle_generation,
    canonical_schema_id,
    declared_generations,
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
    logical_content,
    safe_object_key,
    tree_digest,
)
from docspec.domain.identity import canonical_json_bytes as identity_canonical_json_bytes
from docspec.domain.identity import sha256_digest
from docspec.source_catalog import selected_source_set_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "document_release_v2"
CORPUS_FILE = FIXTURE_ROOT / "corpus.json"

CASES: list[dict[str, Any]] = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))["cases"]
INVALID_CASES = [case for case in CASES if case["expectedCode"] != "valid"]


def _case(name: str) -> dict[str, Any]:
    return next(case for case in CASES if case["name"] == name)


def test_the_sealed_valid_bundle_verifies_with_no_diagnostic_at_all() -> None:
    result = verify_document_release(FIXTURE_ROOT / "valid")

    assert [str(issue) for issue in result.issues] == []
    assert result.valid
    assert result.code == "valid"
    assert result.release_id is not None
    assert result.release_id.startswith("urn:docspec:document-release:v2:")


@pytest.mark.parametrize("case", INVALID_CASES, ids=[case["name"] for case in INVALID_CASES])
def test_each_invalid_bundle_fails_with_exactly_the_diagnostic_it_is_named_for(
    case: dict[str, Any],
) -> None:
    result = verify_document_release(FIXTURE_ROOT / case["bundle"])

    assert not result.valid, f"{case['name']} was accepted"
    assert result.code == case["expectedCode"], [str(issue) for issue in result.issues]
    assert result.path == case["expectedPath"], [str(issue) for issue in result.issues]


def test_the_corpus_spends_exactly_one_bundle_on_every_diagnostic_code() -> None:
    """The codes and the invalid bundles are one list, so neither can grow alone."""

    assert len(CASES) == len(DIAGNOSTIC_CODES) + 1
    assert sorted(case["expectedCode"] for case in INVALID_CASES) == sorted(DIAGNOSTIC_CODES)


def test_verifying_the_corpus_reports_every_bundle_still_sealed_and_as_expected() -> None:
    rows = verify_corpus(CORPUS_FILE)

    assert len(rows) == len(CASES)
    unsealed = [row["name"] for row in rows if not row["sealed"]]
    assert unsealed == [], "a fixture bundle's bytes no longer match its recorded tree digest"
    mismatched = [
        row
        for row in rows
        if row["observedCode"] != row["expectedCode"] or row["observedPath"] != row["expectedPath"]
    ]
    assert mismatched == []


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_bundle_still_digests_to_the_tree_it_was_sealed_under(case: dict[str, Any]) -> None:
    assert tree_digest(FIXTURE_ROOT / case["bundle"]) == case["treeSha256"]


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
    assert len(set(SCHEMA_ID_GENERATIONS)) == 2 * len(SCHEMA_IDS)


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
    assert len(descriptors) == len(SCHEMA_IDS)
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


def test_the_docspec_generation_expects_framed_digests_where_the_corpus_expects_plain(
    tmp_path: Path,
) -> None:
    """The set-digest branch, end to end: the sealed values stop satisfying it."""

    root = _docspec_generation_root()
    bundle = tmp_path / "set-digests"
    shutil.copytree(FIXTURE_ROOT / "valid", bundle)
    (bundle / "release.json").write_bytes(canonical_json_bytes(root))

    result = verify_document_release(bundle)
    refused = {
        issue.path for issue in result.issues if issue.code == "invalid.set-digest"
    }

    assert refused == {
        "release.json/content/selectedSourceSetDigest",
        "release.json/content/documentVersionSetDigest",
        "release.json/content/segmentSetDigest",
        "release.json/content/sourceDocumentMappingDigest",
    }
    documents = json.loads((bundle / "data" / "documents.json").read_text(encoding="utf-8"))
    assert root["content"]["segmentSetDigest"] != framed_set_digest(
        "docspec-document-version-set/2",
        [{"documentVersionId": document["documentVersionId"]} for document in documents],
    )


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
