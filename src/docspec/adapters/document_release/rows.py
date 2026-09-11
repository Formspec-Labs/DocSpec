"""Validate source dispositions, documents, attachments, and comments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from docspec.adapters.document_release.body_index import TextBodyIndex
from docspec.adapters.document_release.capture import _validate_capture, _validate_representation
from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue
from docspec.adapters.document_release.rules import (
    ATTACHMENT_DISPOSITIONS,
    ATTACHMENT_RENDITION_REASON_CODES,
    DOCSPEC_GENERATION,
    NON_SELECTED_DISPOSITIONS,
    SOURCE_DISPOSITION_REASON_CODES,
)
from docspec.domain.identity import stable_urn


def _validate_dispositions(
    dispositions: Sequence[Mapping[str, Any]],
    object_key: str,
    generation: str,
    issues: list[VerificationIssue],
) -> None:
    """Check one row per member of ``U``: identity, and the refusal it declares.

    Amendment C4 adds the vocabulary. A `reasonCode` outside
    `SOURCE_DISPOSITION_REASON_CODES` is refused here, under the docspec
    generation only: the twenty sealed predecessor bundles were minted before any
    such list existed and are not retroactively judged by it. The schema's dotted
    pattern stays the outer bound; this is the inner one, and it is what turns
    "the vocabulary is closed" from a sentence into a check.
    """

    seen_items: set[str] = set()
    seen_documents: set[str] = set()
    for index, row in enumerate(dispositions):
        path = f"{object_key}/{index}"
        source_item_id = row.get("sourceItemId")
        if isinstance(source_item_id, str):
            if source_item_id in seen_items:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/sourceItemId",
                    f"duplicate sourceItemId {source_item_id}",
                )
            seen_items.add(source_item_id)
        disposition = row.get("catalogDisposition")
        if disposition in NON_SELECTED_DISPOSITIONS:
            for field in ("reasonCode", "reason"):
                if not row.get(field):
                    _issue(
                        issues,
                        "invalid.disposition",
                        f"{path}/{field}",
                        f"projected disposition {disposition!r} requires a {field}",
                    )
        reason_code = row.get("reasonCode")
        if (
            generation == DOCSPEC_GENERATION
            and isinstance(reason_code, str)
            and reason_code not in SOURCE_DISPOSITION_REASON_CODES
        ):
            _issue(
                issues,
                "invalid.disposition",
                f"{path}/reasonCode",
                f"{reason_code!r} is not a source-disposition reason code this format declares",
            )
        if disposition == "selected":
            version_id = row.get("documentVersionId")
            if isinstance(version_id, str):
                if version_id in seen_documents:
                    _issue(
                        issues,
                        "invalid.duplicate-identity",
                        f"{path}/documentVersionId",
                        f"duplicate documentVersionId {version_id}",
                    )
                seen_documents.add(version_id)


def _validate_version_binding(
    document: Mapping[str, Any], path: str, issues: list[VerificationIssue]
) -> None:
    """Check what a ``documentVersionId`` embeds against what the row carries.

    Amendment B2. The finding asked for a `documentVersionId` -> `capture.sha256`
    binding "where ids embed `@sha256:`". Measured against the D1 corpus that
    binding is false for 1,683 of 8,082 rows: the Federal Register half embeds
    the rendition digest, the Mirrulations half embeds the catalog's own
    source-issued version, and no expected digest exists there at all. What IS
    true of every row is the convention below, so that is what is enforced --
    plus the finding's own rule, scoped to the rows whose premise holds.
    """

    document_id = document.get("documentId")
    version_id = document.get("documentVersionId")
    issued = document.get("sourceIssuedVersion")
    if isinstance(document_id, str) and isinstance(version_id, str) and isinstance(issued, str):
        expected = f"{document_id}@{issued}"
        if version_id != expected:
            _issue(
                issues,
                "invalid.version-binding",
                f"{path}/documentVersionId",
                f"expected {expected}",
            )
    # Decision 0001's own mint rule: a document body's text body IS its document
    # version, "and a second name would be a second identity for one thing".
    if document.get("textBodyId") != version_id:
        _issue(
            issues,
            "invalid.version-binding",
            f"{path}/textBodyId",
            f"expected {version_id!r}",
        )
    capture = document.get("capture")
    if not isinstance(capture, Mapping) or not isinstance(issued, str):
        return
    if not issued.startswith("sha256:") or capture.get("expectedSha256") is None:
        return
    if issued.removeprefix("sha256:") != capture.get("sha256"):
        _issue(
            issues,
            "invalid.version-binding",
            f"{path}/documentVersionId",
            "the version this id embeds is not the digest of the captured bytes",
        )


def _validate_documents(
    documents: Sequence[Mapping[str, Any]],
    dispositions: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    slices: TextBodyIndex,
    object_key: str,
    key: str,
    generation: str,
    issues: list[VerificationIssue],
) -> dict[str, int]:
    """Check captures and representations. Returns representation byte sizes.

    The returned sizes are keyed by ``key`` -- the field structure and segments
    hang off in this generation -- because that is what the later range checks
    resolve against. For a document body the two names hold the same value; the
    declared one is the one read.
    """

    selected = {
        row["sourceItemId"]: row
        for row in dispositions
        if row.get("catalogDisposition") == "selected" and isinstance(row.get("sourceItemId"), str)
    }
    sizes: dict[str, int] = {}
    seen_versions: set[str] = set()
    for index, document in enumerate(documents):
        path = f"{object_key}/{index}"
        version_id = document.get("documentVersionId")
        if isinstance(version_id, str):
            if version_id in seen_versions:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/documentVersionId",
                    f"duplicate documentVersionId {version_id}",
                )
            seen_versions.add(version_id)
        if generation == DOCSPEC_GENERATION:
            _validate_version_binding(document, path, issues)

        source_item_id = document.get("sourceItemId")
        projection = selected.get(source_item_id) if isinstance(source_item_id, str) else None
        if projection is None:
            _issue(
                issues,
                "invalid.join",
                f"{path}/sourceItemId",
                "document has no selected disposition row",
            )
        else:
            for field in ("documentId", "sourceIssuedVersion"):
                if document.get(field) != projection.get(field):
                    _issue(
                        issues,
                        "invalid.join",
                        f"{path}/{field}",
                        f"differs from the disposition projection ({projection.get(field)!r})",
                    )
            if projection.get("documentVersionId") != version_id:
                _issue(
                    issues,
                    "invalid.join",
                    f"{path}/documentVersionId",
                    "disposition projection names a different document version",
                )

        body_id = document.get(key)
        capture = document.get("capture")
        if isinstance(capture, Mapping):
            _validate_capture(
                capture, member_paths, slices, body_id, f"{path}/capture", issues
            )

        representation = document.get("representation")
        if isinstance(representation, Mapping):
            size = _validate_representation(
                representation, member_paths, slices, body_id, f"{path}/representation", issues
            )
            if size is not None and isinstance(body_id, str):
                sizes[body_id] = size
    return sizes


def _validate_attachments(
    attachments: Sequence[Mapping[str, Any]],
    owners: Mapping[str, str],
    member_paths: Mapping[str, Path],
    index: TextBodyIndex,
    object_key: str,
    issues: list[VerificationIssue],
) -> dict[str, int]:
    """Check the attachment rows, their renditions, and their accounting.

    Amendment A1 governs the identity: ``attachmentId`` is minted over
    ``{ownerTextBodyId, ownerKind, attachmentIdentity}`` and nothing else, so
    re-enumerating an unchanged owner re-mints the same id and the row that
    groups M renditions is not renamed by any one of them.

    Attachments are not members of ``U``, so nothing here blocks a build for a
    rendition that failed: the build fails when an enumerated attachment has no
    row, and a row that honestly says it could not be captured is the accounting
    working. What IS refused is a row that claims text without carrying it, or
    carries text without saying which rendition produced it.
    """

    sizes: dict[str, int] = {}
    seen: set[str] = set()
    for position, attachment in enumerate(attachments):
        path = f"{object_key}/{position}"
        attachment_id = attachment.get("attachmentId")
        if isinstance(attachment_id, str):
            if attachment_id in seen:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/attachmentId",
                    f"duplicate attachmentId {attachment_id}",
                )
            seen.add(attachment_id)
        owner_id = attachment.get("ownerTextBodyId")
        owner_kind = attachment.get("ownerKind")
        if isinstance(owner_id, str) and isinstance(owner_kind, str):
            expected_id = stable_urn(
                "document-release-attachment",
                {
                    "attachmentIdentity": attachment.get("attachmentIdentity"),
                    "ownerKind": owner_kind,
                    "ownerTextBodyId": owner_id,
                },
                version=2,
            )
            if attachment_id != expected_id:
                _issue(
                    issues,
                    "invalid.identity",
                    f"{path}/attachmentId",
                    f"expected {expected_id}",
                )
            owned = owners.get(owner_id)
            if owned is None:
                _issue(
                    issues,
                    "invalid.join",
                    f"{path}/ownerTextBodyId",
                    "attachment names no text body in this release",
                )
            elif owned != owner_kind:
                _issue(
                    issues,
                    "invalid.join",
                    f"{path}/ownerKind",
                    f"owner {owner_id!r} is a {owned!r}, not a {owner_kind!r}",
                )

        renditions = attachment.get("renditions")
        captured: list[int] = []
        if isinstance(renditions, list):
            ordinals: list[int] = []
            for order, rendition in enumerate(renditions):
                if not isinstance(rendition, Mapping):
                    continue
                sub_path = f"{path}/renditions/{order}"
                if isinstance(rendition.get("renditionOrdinal"), int):
                    ordinals.append(rendition["renditionOrdinal"])
                disposition = rendition.get("attachmentDisposition")
                if disposition == "text-captured":
                    captured.append(order)
                elif disposition in ATTACHMENT_DISPOSITIONS:
                    for field in ("reasonCode", "reason"):
                        if not rendition.get(field):
                            _issue(
                                issues,
                                "invalid.attachment-accounting",
                                f"{sub_path}/{field}",
                                f"attachment disposition {disposition!r} requires a {field}",
                            )
                    # Amendment C4: the kebab-case pattern is the outer bound,
                    # this list is the inner one. Attachment rows exist only in
                    # the docspec generation, so no generation guard is needed.
                    reason_code = rendition.get("reasonCode")
                    if (
                        isinstance(reason_code, str)
                        and reason_code not in ATTACHMENT_RENDITION_REASON_CODES
                    ):
                        _issue(
                            issues,
                            "invalid.attachment-accounting",
                            f"{sub_path}/reasonCode",
                            f"{reason_code!r} is not an attachment rendition reason code this "
                            "format declares",
                        )
                else:
                    _issue(
                        issues,
                        "invalid.attachment-accounting",
                        f"{sub_path}/attachmentDisposition",
                        f"{disposition!r} is not one of the four accounted dispositions",
                    )
                capture = rendition.get("capture")
                if isinstance(capture, Mapping):
                    _validate_capture(
                        capture,
                        member_paths,
                        index,
                        # The index addresses one selected rendition per text
                        # body, because that is the slice its row shape names.
                        # Whether an attachment's OTHER renditions are kept as
                        # blobs at all is a recorded open question, so they are
                        # read against the whole member rather than borrowing a
                        # slice that names different bytes.
                        attachment.get("textBodyId") if disposition == "text-captured" else None,
                        f"{sub_path}/capture",
                        issues,
                    )
            if sorted(ordinals) != list(range(len(ordinals))):
                _issue(
                    issues,
                    "invalid.attachment-accounting",
                    f"{path}/renditions",
                    f"rendition ordinals must be dense and zero-based, found {sorted(ordinals)}",
                )
        if not renditions:
            _issue(
                issues,
                "invalid.attachment-accounting",
                f"{path}/renditions",
                "an enumerated attachment carries at least one rendition sub-row",
            )

        # One attachment is one text body, and a text body has exactly one
        # selected representation, so at most one rendition of it can have been
        # text-captured. `textBodyId` names that body and EQUALS `attachmentId`.
        body_id = attachment.get("textBodyId")
        if len(captured) > 1:
            _issue(
                issues,
                "invalid.attachment-accounting",
                f"{path}/renditions",
                f"{len(captured)} renditions claim text-captured; one attachment is one text body",
            )
        if body_id is None:
            if captured:
                _issue(
                    issues,
                    "invalid.attachment-accounting",
                    f"{path}/textBodyId",
                    "a text-captured rendition means this attachment is a text body",
                )
        else:
            if not captured:
                _issue(
                    issues,
                    "invalid.attachment-accounting",
                    f"{path}/textBodyId",
                    "an attachment carrying text must name the rendition that produced it",
                )
            if body_id != attachment_id:
                _issue(
                    issues,
                    "invalid.identity",
                    f"{path}/textBodyId",
                    f"expected {attachment_id!r}",
                )
        representation = attachment.get("representation")
        if isinstance(representation, Mapping):
            size = _validate_representation(
                representation, member_paths, index, body_id, f"{path}/representation", issues
            )
            if size is not None and isinstance(body_id, str):
                sizes[body_id] = size
        elif body_id is not None:
            _issue(
                issues,
                "invalid.representation",
                f"{path}/representation",
                "an attachment carrying text must carry its selected representation",
            )
    return sizes


def _validate_comments(
    comments: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    index: TextBodyIndex,
    object_key: str,
    issues: list[VerificationIssue],
) -> dict[str, int]:
    """Check the comment rows: identity, ownership, and the inherited refusal.

    ``commentId`` equals the catalog's own comment ``sourceRecordId``, so the
    release cannot disagree with the selection it inherits, and ``textBodyId``
    equals it in turn. A comment's owner is exactly one document. The selection
    policy itself is projected verbatim and checked by schema; the tie REFUSAL
    it carries is the upstream owner's, and a repeated comment id here would be
    DocSpec resolving a tie the source owner refused to resolve, so a repeat is
    a duplicate identity rather than a value this release picks between.
    """

    sizes: dict[str, int] = {}
    document_ids = {
        document["documentId"]
        for document in documents
        if isinstance(document.get("documentId"), str)
    }
    seen: set[str] = set()
    # Decision 0001: "DocSpec is handed the already-selected observation per
    # comment id, never a candidate set to reduce", and every row projects one
    # sealed policy verbatim. One release therefore inherits ONE selection; two
    # policy digests in one release is a release claiming a selection nobody
    # sealed, which the schema cannot see because it reads one row at a time.
    inherited: str | None = None
    for position, comment in enumerate(comments):
        path = f"{object_key}/{position}"
        selection = comment.get("commentSelection")
        digest = selection.get("policyDigest") if isinstance(selection, Mapping) else None
        if isinstance(digest, str):
            if inherited is None:
                inherited = digest
            elif digest != inherited:
                _issue(
                    issues,
                    "invalid.comment-selection",
                    f"{path}/commentSelection/policyDigest",
                    f"this release already inherits selection policy {inherited}",
                )
        comment_id = comment.get("commentId")
        if isinstance(comment_id, str):
            if comment_id in seen:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/commentId",
                    f"duplicate commentId {comment_id}",
                )
            seen.add(comment_id)
        body_id = comment.get("textBodyId")
        if body_id != comment_id:
            _issue(issues, "invalid.identity", f"{path}/textBodyId", f"expected {comment_id!r}")
        if comment.get("documentId") not in document_ids:
            _issue(
                issues,
                "invalid.join",
                f"{path}/documentId",
                "comment names no document in this release",
            )
        capture = comment.get("capture")
        if isinstance(capture, Mapping):
            _validate_capture(
                capture, member_paths, index, body_id, f"{path}/capture", issues
            )
        representation = comment.get("representation")
        if isinstance(representation, Mapping):
            size = _validate_representation(
                representation, member_paths, index, body_id, f"{path}/representation", issues
            )
            if size is not None and isinstance(body_id, str):
                sizes[body_id] = size
    return sizes
