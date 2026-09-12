"""Portable release identity, logical digests, canonical encoding, and repacking invariants."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from rulespec_artifacts import FramedSection, framed_section_digest
from rulespec_artifacts import canonical_json_bytes as artifact_canonical_json_bytes

from docspec.adapters.document_release.rules import (
    FRAMED_SET_DOMAINS,
    RELEASE_ID_PREFIX,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
    TABULAR_ROLES,
    expected_document_state_digest,
    expected_release_id,
    framed_set_digest,
)
from docspec.adapters.document_release.verify import (
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
)
from docspec.domain.identity import canonical_json_bytes as identity_canonical_json_bytes
from docspec.domain.identity import sha256_digest
from docspec.source_catalog import selected_source_set_digest
from tests.support.document_release import (
    DOCSPEC_FIXTURE_ROOT,
    DOCSPEC_ROOT,
    DOCSPEC_VALID,
    _root_copy,
    _verify_root_only,
)


def test_the_wire_contract_digest_is_the_unqualified_spelling_of_docspec_identity() -> None:
    value = {"b": [1, 2, {"a": None}], "a": "\u00e9"}

    assert canonical_json_bytes(value) == identity_canonical_json_bytes(value)
    assert sha256_digest(canonical_json_bytes(value)) == f"sha256:{canonical_sha256(value)}"


@pytest.mark.parametrize("jsonl", [False, True])
def test_portable_readers_refuse_unsafe_integers_with_file_context(tmp_path: Path, jsonl: bool) -> None:
    path = tmp_path / ("records.jsonl" if jsonl else "release.json")
    path.write_bytes(b'{"metadata":{"count":9007199254740992}}' + (b"\n" if jsonl else b""))
    read = load_strict_canonical_jsonl if jsonl else load_strict_canonical_json
    with pytest.raises(ValueError, match=path.name):
        read(path)


def test_portable_emission_uses_the_same_safe_integer_boundary() -> None:
    with pytest.raises(ValueError, match="safe"):
        canonical_json_bytes({"count": 2**53})


def test_the_file_digest_is_the_unqualified_spelling_of_the_files_own_bytes() -> None:
    path = DOCSPEC_FIXTURE_ROOT / "valid" / "release.json"

    assert sha256_digest(path.read_bytes()) == f"sha256:{file_sha256(path)}"


def _identity_issues(result: Any) -> list[str]:
    return [str(issue) for issue in result.issues if issue.code == "invalid.identity"]


def test_the_docspec_generation_mints_two_names_over_one_content() -> None:
    root = _root_copy()
    state_digest = expected_document_state_digest(root)

    assert root["documentStateDigest"] == state_digest
    assert state_digest.startswith("sha256:")
    assert root["releaseId"] == RELEASE_ID_PREFIX + state_digest.split(":", 1)[1]
    assert expected_release_id(root) == root["releaseId"]


def test_the_state_digest_is_taken_with_the_containers_canonicaliser() -> None:
    root = _root_copy()
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


def test_a_docspec_generation_root_raises_no_identity_diagnostic(tmp_path: Path) -> None:
    result = _verify_root_only(tmp_path / "docspec-generation", _root_copy())

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
    root = _root_copy()
    root[field] = root[field][:-1] + ("0" if root[field][-1] != "0" else "1")

    result = _verify_root_only(tmp_path / f"tampered-{field}", root)

    assert [issue.path for issue in result.issues if issue.code == "invalid.identity"] == [path]


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
    root = _root_copy()
    repacked = _repacked(root)

    assert repacked["content"] != root["content"]
    assert expected_document_state_digest(repacked) == expected_document_state_digest(root)
    assert expected_release_id(repacked) == root["releaseId"]


def test_a_content_change_still_moves_the_docspec_state_digest() -> None:
    """The exclusion is physical facts, not "anything that is not a row"."""

    root = _root_copy()
    for mutate in (
        lambda value: value["content"]["counts"].__setitem__("selectedCount", 99),
        lambda value: value["content"]["coverage"].__setitem__("representationByteTotal", 1),
        lambda value: value["content"].__setitem__("corpusId", "urn:docspec:document-corpus:other"),
    ):
        mutated = json.loads(json.dumps(root))
        mutate(mutated)
        assert expected_document_state_digest(mutated) != root["documentStateDigest"]


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


def test_the_two_encoders_agree_byte_for_byte_on_this_formats_domain() -> None:
    """D2's surviving factual basis, asserted rather than cited.

    The container's canonicaliser and DocSpec's identity canonicaliser emit
    identical bytes for every value this format actually carries: the sealed
    valid root, its content object, and the logical payload the docspec
    generation digests. Where they differ (non-BMP object keys, refusal
    surfaces) is outside this format's domain.
    """
    root = DOCSPEC_ROOT
    logical = {
        "format": root["format"],
        "formatVersion": root["formatVersion"],
        "logicalContent": logical_content(root["content"]),
    }
    for value in (root, root["content"], logical):
        assert artifact_canonical_json_bytes(value) == identity_canonical_json_bytes(value)


def test_a_physical_only_repack_of_the_real_bundle_preserves_its_state_digest() -> None:
    """C11b on a minted bundle rather than on a synthetic root."""

    repacked = _repacked(DOCSPEC_ROOT)

    assert repacked["content"] != DOCSPEC_ROOT["content"]
    assert expected_document_state_digest(repacked) == DOCSPEC_ROOT["documentStateDigest"]
    assert (
        expected_release_id(repacked)
        == DOCSPEC_ROOT["releaseId"]
    )


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


def test_the_first_mints_blind_gate_attack_now_moves_the_release_name(
    tmp_path: Path,
) -> None:
    """Amendment B1, end to end on a real bundle rather than on synthetic rows.

    The attack the first mint's gate did not catch: change a body's
    representation bytes WITHOUT changing their length, restamp every physical
    digest the change invalidates -- the member digest, the manifest, the index
    slice, the row's own `representation.sha256` -- and see whether the release
    still calls itself the same release.

    Under the `/2` domains it did, because a set digest framed each row's id
    fields and nothing in the identity preimage had moved. Under `/3` the name
    moves, the mutated bundle verifies clean under that different name -- it IS
    a different corpus, and now says so -- and the id-only projection is checked
    beside it to show the hole was real rather than imagined.
    """

    from tools.restamp_document_release_fixtures import _restamp, _state

    bundle = tmp_path / "same-length-mutation"
    shutil.copytree(DOCSPEC_VALID, bundle)
    sealed_root = load_strict_canonical_json(bundle / "release.json")
    sealed_documents = load_strict_canonical_jsonl(bundle / "data" / "documents.jsonl")

    target = sealed_documents[0]["representation"]["objectKey"]
    before = (bundle / target).read_bytes()
    after = before.replace(b"Salmonella", b"SALMONELLA")
    assert after != before and len(after) == len(before)
    (bundle / target).write_bytes(after)

    state = _state(bundle)
    body = state["documents"][0]["textBodyId"]
    row = next(
        entry
        for entry in state["textBodyIndex"]
        if entry["family"] == "text" and entry["textBodyId"] == body
    )
    state["documents"][0]["representation"]["sha256"] = hashlib.sha256(
        after[row["startByte"] : row["startByte"] + row["byteLength"]]
    ).hexdigest()
    _restamp(bundle, state)

    mutated_root = load_strict_canonical_json(bundle / "release.json")
    mutated_documents = load_strict_canonical_jsonl(bundle / "data" / "documents.jsonl")

    def id_only(rows: list[dict[str, Any]]) -> str:
        records = sorted(
            ({"documentVersionId": entry["documentVersionId"]} for entry in rows),
            key=lambda record: record["documentVersionId"].encode("utf-16-be"),
        )
        return framed_section_digest(
            "docspec-document-version-set/2",
            (FramedSection("members", len(records), iter(records)),),
        )

    # The hole, reproduced: what `/2` framed is unmoved by this mutation.
    assert id_only(sealed_documents) == id_only(mutated_documents)
    # And closed: what `/3` frames is not.
    assert framed_set_digest(
        "docspec-document-version-set/3", sealed_documents
    ) != framed_set_digest("docspec-document-version-set/3", mutated_documents)
    assert mutated_root["documentStateDigest"] != sealed_root["documentStateDigest"]
    assert mutated_root["releaseId"] != sealed_root["releaseId"]
    assert verify_document_release(bundle).valid
