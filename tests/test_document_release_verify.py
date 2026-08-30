"""Exercise the ported DocumentRelease 2.0 verifier against the whole sealed corpus.

The corpus is one mutation per diagnostic code: the valid bundle must verify,
and each invalid bundle must fail with exactly the code and path it is named
for. Anything less than every bundle leaves the fixtures inert -- present,
digest-sealed, and never actually run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.document_release_verify import (
    DIAGNOSTIC_CODES,
    SCHEMA_FILES,
    SCHEMA_ID_GENERATIONS,
    SCHEMA_IDS,
    canonical_schema_id,
    verify_corpus,
    verify_document_release,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_strict_canonical_json,
    safe_object_key,
    tree_digest,
)
from docspec.domain.identity import canonical_json_bytes as identity_canonical_json_bytes
from docspec.domain.identity import sha256_digest

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
