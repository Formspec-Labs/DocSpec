from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from docspec.domain.identity import parse_canonical_json, require_relative_path
from docspec.errors import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "src" / "docspec" / "schemas" / "document_release" / "2.0"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "document_release_v2"

SCHEMA_IDS = {
    "document-release.schema.json": "urn:docspec:schema:document-release:2.0",
    "member-manifest.schema.json": "urn:docspec:schema:document-release-member-manifest:2.0",
    "source-dispositions.schema.json": "urn:docspec:schema:document-release-source-dispositions:2.0",
    "documents.schema.json": "urn:docspec:schema:document-release-documents:2.0",
    "structural-nodes.schema.json": "urn:docspec:schema:document-release-structural-nodes:2.0",
    "search-segments.schema.json": "urn:docspec:schema:document-release-search-segments:2.0",
}


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_closed_object)


def _schema(name: str) -> dict[str, Any]:
    value = _load(SCHEMA_DIR / name)
    assert isinstance(value, dict)
    return value


def _root_validator() -> Draft202012Validator:
    return Draft202012Validator(_schema("document-release.schema.json"))


def _embedded(bundle: str, name: str) -> dict[str, Any]:
    """One schema as the sealed bundle carries it.

    The packaged schemas are the DOCSPEC minting generation now that Decision
    0001's restamp has landed on them, and the twenty sealed bundles were minted
    under the predecessor generation. Their schema bodies are not packaged
    anywhere else -- they live in the bundles that carry them, digest-pinned by
    their own descriptors -- so a predecessor fixture is checked against its own
    embedded copy. A bundle is read as it was written.
    """

    value = _load(FIXTURE_DIR / bundle / "schemas" / name)
    assert isinstance(value, dict)
    return value


def _errors(validator: Draft202012Validator, value: Any) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    ]


def test_the_two_zero_schema_bundle_is_complete_and_every_schema_is_valid_json_schema() -> None:
    packaged = sorted(path.name for path in SCHEMA_DIR.glob("*.schema.json"))
    assert packaged == sorted(SCHEMA_IDS)

    for name, schema_id in SCHEMA_IDS.items():
        schema = _schema(name)
        Draft202012Validator.check_schema(schema)
        # The port re-homed every identifier; no rulespec-flavored $id may survive.
        assert schema["$id"] == schema_id
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "rulespec.org" not in (SCHEMA_DIR / name).read_text(encoding="utf-8")


def test_the_valid_fixture_bundle_satisfies_the_root_schema_it_was_sealed_under() -> None:
    release = _load(FIXTURE_DIR / "valid" / "release.json")
    sealed_root_schema = Draft202012Validator(
        _embedded("valid", "document-release-v2.schema.json")
    )

    assert _errors(sealed_root_schema, release) == []
    assert release["format"] == "docspec-document-release"
    assert release["formatVersion"] == "2.0"


def test_the_packaged_root_schema_no_longer_describes_the_predecessor_corpus() -> None:
    """The restamp moved the packaged generation, and that is visible from here.

    Decision 0001's items 6, 8, and 9 reshaped the root: `documentStateDigest`
    is required, `processingPolicy` became `processingPolicies`, and the catalog
    pin became `{catalogId, catalogDigest}`. The sealed corpus predates all
    three, so the packaged schema must REFUSE it. If this ever passes again,
    either the restamp was reverted or the sealed corpus was rewritten.
    """

    release = _load(FIXTURE_DIR / "valid" / "release.json")
    errors = _errors(_root_validator(), release)

    assert ": 'documentStateDigest' is a required property" in errors
    assert any("processingPolicies" in item for item in errors), errors


def test_the_valid_fixture_root_is_exact_canonical_json_without_a_trailing_newline() -> None:
    # Deviation row 4: 2.0 root bytes are canonical JSON in non-file form. DocSpec's
    # live `canonical_json_file_bytes` appends the newline this format forbids.
    data = (FIXTURE_DIR / "valid" / "release.json").read_bytes()
    assert not data.endswith(b"\n")
    assert parse_canonical_json(data, label="release.json", file_form=False)


def test_every_fixture_bundle_is_present_and_named_by_the_sealed_corpus() -> None:
    corpus = _load(FIXTURE_DIR / "corpus.json")
    bundles = {case["bundle"] for case in corpus["cases"]}
    assert len(corpus["cases"]) == 20
    assert bundles == {"valid"} | {f"invalid/{path.name}" for path in (FIXTURE_DIR / "invalid").iterdir()}
    for case in corpus["cases"]:
        assert (FIXTURE_DIR / case["bundle"] / "release.json").is_file()


def test_the_unknown_version_fixture_is_refused_by_the_root_schema() -> None:
    release = _load(FIXTURE_DIR / "invalid" / "unknown-version" / "release.json")
    errors = _errors(_root_validator(), release)
    assert any(item.startswith("formatVersion:") for item in errors), errors


def test_the_unknown_node_kind_fixture_is_refused_by_the_structural_node_schema() -> None:
    validator = Draft202012Validator(
        _embedded("valid", "structural-nodes-v1.schema.json")
    )
    rows = _load(FIXTURE_DIR / "invalid" / "unknown-node-kind" / "data" / "structural-nodes.json")
    refused = [row for row in rows if _errors(validator, row)]
    assert refused, "no structural node row carried the unknown nodeKind"
    assert all(_errors(validator, row) == [] for row in _load(
        FIXTURE_DIR / "valid" / "data" / "structural-nodes.json"
    ))


def test_the_noncanonical_root_fixture_is_refused_as_non_canonical_bytes() -> None:
    data = (FIXTURE_DIR / "invalid" / "noncanonical-root" / "release.json").read_bytes()
    with pytest.raises(IntegrityError):
        parse_canonical_json(data, label="release.json", file_form=False)


def test_the_unsafe_path_fixture_is_refused_by_the_relative_path_rule() -> None:
    manifest = _load(FIXTURE_DIR / "invalid" / "unsafe-path" / "manifests" / "global.json")
    unsafe = []
    for member in manifest["members"]:
        try:
            require_relative_path(member["objectKey"], "objectKey")
        except ValueError:
            unsafe.append(member["objectKey"])
    assert unsafe == ["../escaped-search-segments.json"]

    clean = _load(FIXTURE_DIR / "valid" / "manifests" / "global.json")
    for member in clean["members"]:
        require_relative_path(member["objectKey"], "objectKey")
