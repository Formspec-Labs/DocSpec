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
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "document_release_v2_docspec"

SCHEMA_IDS = {
    "document-release.schema.json": "urn:docspec:schema:document-release:2.0",
    "member-manifest.schema.json": "urn:docspec:schema:document-release-member-manifest:2.0",
    "source-dispositions.schema.json": "urn:docspec:schema:document-release-source-dispositions:2.0",
    "documents.schema.json": "urn:docspec:schema:document-release-documents:2.0",
    "attachments.schema.json": "urn:docspec:schema:document-release-attachments:2.0",
    "comments.schema.json": "urn:docspec:schema:document-release-comments:2.0",
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


def _errors(validator: Draft202012Validator, value: Any) -> list[str]:
    return [
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    ]


def test_the_two_zero_schema_bundle_is_complete_and_every_schema_is_valid_json_schema() -> None:
    packaged = sorted(path.name for path in SCHEMA_DIR.glob("*.schema.json"))
    # Eight since restamp item 3's 6 -> 8 widening: nothing else may be sitting
    # in the packaged directory, and nothing named here may be missing from it.
    assert packaged == sorted(SCHEMA_IDS)
    assert len(SCHEMA_IDS) == 8

    for name, schema_id in SCHEMA_IDS.items():
        schema = _schema(name)
        Draft202012Validator.check_schema(schema)
        # The port re-homed every identifier; no rulespec-flavored $id may survive.
        assert schema["$id"] == schema_id
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "rulespec.org" not in (SCHEMA_DIR / name).read_text(encoding="utf-8")


def test_the_unknown_version_fixture_is_refused_by_the_root_schema() -> None:
    release = _load(FIXTURE_DIR / "invalid" / "unknown-version" / "release.json")
    errors = _errors(_root_validator(), release)
    assert any(item.startswith("formatVersion:") for item in errors), errors


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
    assert unsafe == ["../escaped-search-segments.jsonl"]

    clean = _load(FIXTURE_DIR / "valid" / "manifests" / "global.json")
    for member in clean["members"]:
        require_relative_path(member["objectKey"], "objectKey")


# ─── The docspec generation the packaged schemas now describe ──────────


def test_the_docspec_valid_bundle_satisfies_the_packaged_root_schema() -> None:
    """The packaged schemas ARE the docspec generation, so its corpus fits them."""

    release = _load(FIXTURE_DIR / "valid" / "release.json")

    assert _errors(_root_validator(), release) == []
    assert release["format"] == "docspec-document-release"
    assert release["formatVersion"] == "2.0"
    assert release["documentStateDigest"].startswith("sha256:")


def test_the_docspec_valid_root_is_exact_canonical_json_without_a_trailing_newline() -> None:
    data = (FIXTURE_DIR / "valid" / "release.json").read_bytes()

    assert not data.endswith(b"\n")
    assert parse_canonical_json(data, label="release.json", file_form=False)


def test_every_docspec_fixture_bundle_is_present_and_named_by_its_sealed_corpus() -> None:
    corpus = _load(FIXTURE_DIR / "corpus.json")
    bundles = {case["bundle"] for case in corpus["cases"]}

    # Twenty from the restamp, plus four for amendments B1/B2/B4, plus five for
    # amendments C1/C3/C4/C6 -- four rules those turned from prose into checks,
    # and the comment-selection case that could not be minted until C6 gave the
    # corpus a comment.
    assert len(corpus["cases"]) == 29
    assert bundles == {"valid"} | {
        f"invalid/{path.name}" for path in (FIXTURE_DIR / "invalid").iterdir()
    }
    for case in corpus["cases"]:
        assert (FIXTURE_DIR / case["bundle"] / "release.json").is_file()


def test_the_docspec_unknown_node_kind_fixture_is_refused_by_the_packaged_schema() -> None:
    validator = Draft202012Validator(_schema("structural-nodes.schema.json"))
    rows = [
        json.loads(line)
        for line in (
            FIXTURE_DIR / "invalid" / "unknown-node-kind" / "data" / "structural-nodes.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    refused = [row for row in rows if _errors(validator, row)]

    assert refused, "no structural node row carried the unknown nodeKind"
    valid_rows = [
        json.loads(line)
        for line in (FIXTURE_DIR / "valid" / "data" / "structural-nodes.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert all(_errors(validator, row) == [] for row in valid_rows)
