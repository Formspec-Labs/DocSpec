"""The minted portable release schemas, row shape, vocabularies, and reproducible fixture bytes."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from docspec.adapters.document_release.rules import (
    ALLOWED_MEMBER_ROLES,
    RELEASE_ID_PREFIX,
    SCHEMA_FILES,
    SCHEMA_IDS,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
    TEXT_BODY_SET_DOMAIN,
    TEXT_KINDS,
    expected_document_state_digest,
    expected_release_id,
    framed_set_digest,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    load_strict_canonical_json,
    logical_content,
)
from docspec.domain.storage import partition_bucket
from tests.support.document_release import (
    DOCSPEC_CORPUS_FILE,
    DOCSPEC_FIXTURE_ROOT,
    DOCSPEC_ROOT,
    DOCSPEC_VALID,
    PREDECESSOR_FIXTURE_ROOT,
    SOURCE_CATALOG_FIXTURE,
    _members,
    _rows,
)


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


def test_item_13_the_manifest_role_vocabulary_gained_the_two_tabular_roles() -> None:
    """The enum widened at item 13; the gate has now caught up with it."""

    schema = json.loads(
        (SCHEMA_FILES["member-manifest"]).read_text(encoding="utf-8")
    )
    roles = schema["$defs"]["memberDescriptor"]["properties"]["role"]["enum"]

    assert "attachments" in roles
    assert "comments" in roles
    assert {"attachments", "comments"} <= ALLOWED_MEMBER_ROLES
    assert set(roles) == ALLOWED_MEMBER_ROLES


@pytest.mark.parametrize("name", ["documents", "structural-nodes", "search-segments"])
def test_items_4_and_5_every_row_carries_the_text_body_key_and_kind(name: str) -> None:
    rows = _rows(name)

    assert rows
    for row in rows:
        assert row["textBodyId"]
        # One text pipeline, three kinds, and since amendment C6 the corpus
        # mints all three: an attachment (B4) and a comment (C6) are text bodies
        # like any document body, and structure and segments hang off them
        # through the same key.
        assert row["textKind"] in TEXT_KINDS
    if name == "documents":
        assert {row["textKind"] for row in rows} == {"document-body"}
    else:
        assert {row["textKind"] for row in rows} == set(TEXT_KINDS)
    if name != "documents":
        # Re-keyed, not merely widened: the old key is gone from these members.
        assert all("documentVersionId" not in row for row in rows)


def test_the_text_body_id_of_a_document_body_equals_its_document_version_id() -> None:
    """Decision 0001's mint rule: one body per version, never a second name."""

    for document in _rows("documents"):
        assert document["textBodyId"] == document["documentVersionId"]
    bodies = {document["textBodyId"] for document in _rows("documents")}
    bodies |= {
        row["textBodyId"]
        for row in [*_rows("attachments"), *_rows("comments")]
        if row["textBodyId"] is not None
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
    comments = _rows("comments")

    assert content["textBodySetDigest"] == framed_set_digest(
        TEXT_BODY_SET_DOMAIN,
        [
            {"textBodyId": row["textBodyId"], "textKind": row["textKind"]}
            for row in [*_rows("documents"), *attachments, *comments]
            if row["textBodyId"] is not None
        ],
    )
    # Both digests stream the FULL rows (amendment B1), and since amendment C6
    # neither streams the empty set: the corpus mints an attachment and a
    # comment, so a digest that was a constant is now a measurement.
    assert content["attachmentSetDigest"] == framed_set_digest(
        "docspec-attachment-set/3", attachments
    )
    assert content["commentSetDigest"] == framed_set_digest(
        "docspec-comment-set/3", comments
    )
    assert comments
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


def test_the_current_corpus_uses_the_declared_framed_domains() -> None:
    """Mapping and selected-source digests follow their declared domains."""

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
    assert restamper.PREDECESSOR_FIXTURE_ROOT == PREDECESSOR_FIXTURE_ROOT
    assert restamper.CORPUS_FILE == DOCSPEC_CORPUS_FILE
