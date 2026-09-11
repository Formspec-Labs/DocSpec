"""Shared portable release bundles and canonical row setup."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.document_release.verify import (
    verify_document_release,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
)
from docspec.domain.identity import stable_urn
from docspec.domain.storage import partition_bucket
from tools.restamp_document_release_fixtures import COMMENT_SELECTION_POLICY_DIGEST


ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "document_release_v2"
DOCSPEC_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "document_release_v2_docspec"
DOCSPEC_CORPUS_FILE = DOCSPEC_FIXTURE_ROOT / "corpus.json"


def _cases(corpus_file: Path) -> list[dict[str, Any]]:
    return json.loads(corpus_file.read_text(encoding="utf-8"))["cases"]


PREDECESSOR_CASES = _cases(PREDECESSOR_FIXTURE_ROOT / "corpus.json")
DOCSPEC_CASES = _cases(DOCSPEC_CORPUS_FILE)
INVALID_CASES = [case for case in DOCSPEC_CASES if case["expectedCode"] != "valid"]
DOCSPEC_VALID = DOCSPEC_FIXTURE_ROOT / "valid"
DOCSPEC_ROOT: dict[str, Any] = load_strict_canonical_json(DOCSPEC_VALID / "release.json")
DOCSPEC_MANIFEST: dict[str, Any] = load_strict_canonical_json(
    DOCSPEC_VALID / "manifests" / "global.json"
)
SOURCE_CATALOG_FIXTURE = ROOT / "tests" / "fixtures" / "source_catalog_release_v1" / "valid"


def _root_copy() -> dict[str, Any]:
    return json.loads(json.dumps(DOCSPEC_ROOT))


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


def _rows(name: str) -> list[dict[str, Any]]:
    return load_strict_canonical_jsonl(DOCSPEC_VALID / "data" / f"{name}.jsonl")


def _members(role: str) -> list[dict[str, Any]]:
    return [member for member in DOCSPEC_MANIFEST["members"] if member["role"] == role]


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
    # Appended, not replaced. Since amendment C6 the sealed bundle carries a
    # comment of its own, and dropping it here would leave its text and blob
    # slices in the index with no row naming them -- the same reason the
    # attachments below are appended. It also puts two comments under ONE sealed
    # selection policy in front of the gate, which is the shape a real release
    # has and the single-comment corpus cannot show; the policy digest is
    # therefore the corpus's, since two digests would be the defect
    # `test_two_selection_policies_in_one_release_is_a_comment_selection_defect`
    # exists to provoke deliberately.
    state["comments"] += [
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
                "policyDigest": COMMENT_SELECTION_POLICY_DIGEST,
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
                    # The enumerated bytes were never preserved, so there is no
                    # capture to carry and the loss is recorded rather than
                    # silently dropped. The code is B7's, not an invention:
                    # amendment C4 made the vocabulary a check, and a test that
                    # spelt its own code was the first thing it caught.
                    "attachmentDisposition": "source-unavailable",
                    "capture": None,
                    "mediaType": "application/pdf",
                    "reason": "The pinned checkpoint preserved no copy of the enumerated exhibit.",
                    "reasonCode": "no-preserved-copy",
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
    # The comment kind's policy is no longer added here: since amendment C6 the
    # sealed corpus declares one, because it mints a comment. Adding a second
    # copy would put a redundant row in a table whose whole job is to be joined.
    _restamp(bundle, state)
    return bundle, state


@pytest.fixture
def extended(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    return _extended_bundle(tmp_path)
