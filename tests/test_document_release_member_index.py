"""Text-body index coverage, partition slices, and bounded member reads."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.document_release.rules import (
    SCHEMA_FILES,
    SCHEMA_IDS,
    TEXT_BODY_INDEX_ROLE,
    TEXT_BODY_INDEX_ROW_DEF,
    stamp_root,
)
from docspec.adapters.document_release.verify import (
    verify_document_release,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    file_sha256,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
    write_canonical_jsonl,
)
from docspec.domain.storage import partition_bucket
from tests.support.document_release import (
    COMMENT_ID,
    DOCSPEC_ROOT,
    DOCSPEC_VALID,
    _extended_bundle,
    _members,
    _rows,
)
from tests.support.document_release import (
    extended as _extended_fixture,
)


# Register the shared fixture in this suite.
extended = _extended_fixture


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
    bodies += [row["textBodyId"] for row in _rows("comments")]
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
    from docspec.adapters.document_release.members import _read_slice

    path = tmp_path / "member"
    path.write_bytes(b"0123456789")

    assert _read_slice(path, 2, 3) == b"234"
    for start, length in ((-1, 3), (2, -3)):
        with pytest.raises(ValueError, match="non-negative"):
            _read_slice(path, start, length)
