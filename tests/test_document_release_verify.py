"""Current portable release admission, complete corpus diagnostics, and parser refusals."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.document_release.diagnostics import (
    DIAGNOSTIC_CODES,
)
from docspec.adapters.document_release.rules import (
    SCHEMA_FILES,
    stamp_root,
)
from docspec.adapters.document_release.verify import (
    verify_corpus,
    verify_document_release,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    canonical_sha256,
    load_strict_canonical_json,
    safe_object_key,
    tree_digest,
)
from tests.support.document_release import (
    DOCSPEC_CASES,
    DOCSPEC_CORPUS_FILE,
    DOCSPEC_FIXTURE_ROOT,
    DOCSPEC_VALID,
    INVALID_CASES,
    PREDECESSOR_CASES,
    PREDECESSOR_FIXTURE_ROOT,
    _root_copy,
    _verify_root_only,
)


def test_the_valid_bundle_verifies_with_no_diagnostic_at_all() -> None:
    result = verify_document_release(DOCSPEC_FIXTURE_ROOT / "valid")

    assert [str(issue) for issue in result.issues] == []
    assert result.valid
    assert result.code == "valid"
    assert result.release_id is not None
    assert result.release_id.startswith("urn:docspec:document-release:v2:")


@pytest.mark.parametrize("case", INVALID_CASES, ids=[case["name"] for case in INVALID_CASES])
def test_each_invalid_bundle_fails_with_exactly_the_diagnostic_it_is_named_for(
    case: dict[str, Any],
) -> None:
    result = verify_document_release(DOCSPEC_FIXTURE_ROOT / case["bundle"])

    assert not result.valid, f"{case['name']} was accepted"
    assert result.code == case["expectedCode"], [str(issue) for issue in result.issues]
    assert result.path == case["expectedPath"], [str(issue) for issue in result.issues]


# A second fixture under one code proves a distinct rule, not another spelling:
# B1's repeated key; B4/C1's ungoverned media type; C3's selected-source digest;
# and C4's source and attachment reason vocabularies.
CODES_WITH_A_SECOND_RULE = frozenset(
    {
        "invalid.duplicate-identity",
        "invalid.retention-floor",
        "invalid.set-digest",
        "invalid.disposition",
        "invalid.attachment-accounting",
    }
)


def test_the_corpus_covers_every_diagnostic_code() -> None:
    """The codes and invalid bundles stay one complete list."""

    covered = {case["expectedCode"] for case in INVALID_CASES}
    assert covered == set(DIAGNOSTIC_CODES)
    spent = [case["expectedCode"] for case in INVALID_CASES]
    repeated = {code for code in spent if spent.count(code) > 1}
    assert repeated <= CODES_WITH_A_SECOND_RULE


def test_every_case_seals_the_whole_diagnostic_set_it_produces() -> None:
    """A new secondary diagnostic must fail just as a changed primary one does."""

    for case in DOCSPEC_CASES:
        result = verify_document_release(DOCSPEC_FIXTURE_ROOT / case["bundle"])
        observed = [{"code": issue.code, "path": issue.path} for issue in result.issues]
        assert observed == case["expectedDiagnostics"], case["name"]


def test_verifying_the_corpus_reports_every_bundle_sealed_and_as_expected() -> None:
    rows = verify_corpus(DOCSPEC_CORPUS_FILE)

    assert len(rows) == len(DOCSPEC_CASES)
    assert [row["name"] for row in rows if not row["sealed"]] == []
    assert [
        row
        for row in rows
        if row["observedCode"] != row["expectedCode"] or row["observedPath"] != row["expectedPath"]
    ] == []


def test_the_frozen_predecessor_corpus_keeps_its_provenance_seals() -> None:
    """Preserve historic fixture bytes without supporting their reader rules."""

    assert len(PREDECESSOR_CASES) == 20
    for case in PREDECESSOR_CASES:
        assert tree_digest(PREDECESSOR_FIXTURE_ROOT / case["bundle"]) == case["treeSha256"]


def test_the_current_corpus_preserves_the_predecessors_diagnostic_cases() -> None:
    """The restamp retained its original rules and added the amendments' cases."""

    predecessor_names = [case["name"] for case in PREDECESSOR_CASES]
    kept = [case for case in DOCSPEC_CASES if case["name"] in predecessor_names]
    assert [(case["name"], case["expectedCode"]) for case in kept] == [
        (case["name"], case["expectedCode"]) for case in PREDECESSOR_CASES
    ]
    assert [case["name"] for case in DOCSPEC_CASES if case["name"] not in predecessor_names] == [
        "version-binding",
        "attachment-accounting",
        "duplicate-attachment",
        "retention-floor",
        "comment-selection",
        "ungoverned-media-type",
        "selected-source-set-digest",
        "unknown-disposition-reason-code",
        "unknown-attachment-reason-code",
    ]


def test_a_frozen_predecessor_bundle_is_unsupported() -> None:
    result = verify_document_release(PREDECESSOR_FIXTURE_ROOT / "valid")

    assert not result.valid
    assert any(
        issue.code == "invalid.schema" and "https://rulespec.org/schemas/releases/" in issue.message
        for issue in result.issues
    ), [str(issue) for issue in result.issues]


@pytest.mark.parametrize(
    "schema_id",
    [
        "https://rulespec.org/schemas/releases/document-release-v2.schema.json",
        "urn:docspec:schema:document-release:9.9",
    ],
)
def test_an_unregistered_schema_identifier_is_refused(tmp_path: Path, schema_id: str) -> None:
    root = _root_copy()
    descriptor = next(
        item for item in root["content"]["schemaSet"]["schemas"] if item["roles"] == ["release-root"]
    )
    descriptor["schemaId"] = schema_id

    result = _verify_root_only(tmp_path / "unsupported-schema", stamp_root(root))

    assert not result.valid
    assert any(
        issue.code == "invalid.schema"
        and issue.path.startswith("release.json/content/schemaSet/schemas/")
        and schema_id in issue.message
        for issue in result.issues
    ), [str(issue) for issue in result.issues]


@pytest.mark.parametrize(
    ("schema_set", "expected_path"),
    [
        pytest.param("missing", "release.json/content/schemaSet", id="missing-schema-set"),
        pytest.param(None, "release.json/content/schemaSet", id="null-schema-set"),
        pytest.param({}, "release.json/content/schemaSet/schemas", id="missing-schemas"),
        pytest.param({"schemas": "eight"}, "release.json/content/schemaSet/schemas", id="non-array-schemas"),
    ],
)
def test_an_unreadable_schema_set_refuses_a_complete_bundle(
    tmp_path: Path, schema_set: Any, expected_path: str,
) -> None:
    bundle = tmp_path / "unreadable-schema-set"
    shutil.copytree(DOCSPEC_VALID, bundle)
    root = _root_copy()
    if schema_set == "missing":
        root["content"].pop("schemaSet")
    else:
        root["content"]["schemaSet"] = schema_set
    (bundle / "release.json").write_bytes(canonical_json_bytes(stamp_root(root)))

    result = verify_document_release(bundle)

    assert not result.valid
    assert [(issue.code, issue.path) for issue in result.issues] == [("invalid.schema", expected_path)]


def test_each_bundle_embedded_schema_still_carries_the_id_its_descriptor_names() -> None:
    """Each schema descriptor binds the registered ID to the carried bytes."""

    root = load_strict_canonical_json(DOCSPEC_FIXTURE_ROOT / "valid" / "release.json")
    manifest = load_strict_canonical_json(DOCSPEC_FIXTURE_ROOT / "valid" / "manifests" / "global.json")
    members = {
        member["schemaId"]: member
        for member in manifest["members"]
        if member["role"] == "schema"
    }

    descriptors = root["content"]["schemaSet"]["schemas"]
    assert len(descriptors) == len(SCHEMA_FILES)
    for descriptor in descriptors:
        member = members[descriptor["schemaId"]]
        embedded = json.loads(
            (DOCSPEC_FIXTURE_ROOT / "valid" / member["objectKey"]).read_text(encoding="utf-8")
        )
        assert embedded["$id"] == descriptor["schemaId"]
        assert member["sha256"] == descriptor["schemaSha256"]
    assert root["content"]["schemaSet"]["schemaSetId"] == (
        f"urn:spicy:schema-set:v1:{canonical_sha256(descriptors)}"
    )


def test_strict_loading_returns_mutable_json_and_refuses_a_trailing_newline() -> None:
    """2.0 root bytes are canonical JSON in non-file form; the verifier mutates rows."""

    root = load_strict_canonical_json(DOCSPEC_FIXTURE_ROOT / "valid" / "release.json")

    assert isinstance(root, dict)
    assert isinstance(root["content"]["schemaSet"]["schemas"], list)
    with pytest.raises(ValueError):
        load_strict_canonical_json(DOCSPEC_FIXTURE_ROOT / "invalid" / "noncanonical-root" / "release.json")


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


def test_the_gate_always_produces_a_verdict_even_when_a_rule_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that can be crashed is a gate that can be skipped (amendment B3)."""

    import docspec.adapters.document_release.verify as module

    def explode(_bundle: Path) -> Any:
        raise RuntimeError("a rule reached an unreachable state")

    monkeypatch.setattr(module, "_verify_document_release", explode)
    result = module.verify_document_release(tmp_path)

    assert not result.valid
    assert result.code == "invalid.root-syntax"
    assert result.path == "release.json"
    assert "RuntimeError" in result.issues[0].message
