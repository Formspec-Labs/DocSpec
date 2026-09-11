"""Reconcile root identities, counts, and coverage with member contents."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from docspec.adapters.document_release.coverage import derive_counts, derive_coverage
from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue
from docspec.adapters.document_release.rules import (
    CATALOG_STATE_REASON_CODES,
    SELECTED_SOURCE_SET_DOMAIN,
    SOURCE_TO_DOCUMENT_DOMAIN,
    TEXT_BODY_SET_DOMAIN,
    TEXT_KINDS,
    framed_set_digest,
)


def _validate_root_bindings(
    root: Mapping[str, Any],
    dispositions: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]],
    documents_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> None:
    content = root.get("content")
    if not isinstance(content, Mapping):
        return

    selected_ids = [
        row["sourceItemId"]
        for row in dispositions
        if row.get("catalogDisposition") == "selected" and isinstance(row.get("sourceItemId"), str)
    ]
    version_ids = [
        document["documentVersionId"]
        for document in documents
        if isinstance(document.get("documentVersionId"), str)
    ]
    joined = [
        document
        for document in documents
        if isinstance(document.get("sourceItemId"), str)
        and isinstance(document.get("documentId"), str)
        and isinstance(document.get("documentVersionId"), str)
    ]
    text_bodies = [
        {"textBodyId": document[key], "textKind": document["textKind"]}
        for document in documents
        if isinstance(document.get(key), str) and isinstance(document.get("textKind"), str)
    ]
    # One text pipeline, three kinds: the text-body set spans every kind that
    # carries text. An attachment whose renditions all failed carries none and
    # is not a member of it -- it is accounted in its own row, not here.
    text_bodies += [
        {"textBodyId": row["textBodyId"], "textKind": row["textKind"]}
        for row in [*attachments, *comments]
        if isinstance(row.get("textBodyId"), str) and isinstance(row.get("textKind"), str)
    ]

    catalog = content.get("sourceCatalog")
    if isinstance(catalog, Mapping):
        pinned = catalog.get("catalogId")
        for index, document in enumerate(documents):
            capture = document.get("capture")
            if isinstance(capture, Mapping) and capture.get("catalogReleaseId") != pinned:
                _issue(
                    issues,
                    "invalid.source-catalog-pin",
                    f"{documents_key}/{index}/capture/catalogReleaseId",
                    f"capture names a different catalog release than the root pin {pinned!r}",
                )

    digest_plan: tuple[tuple[str, Callable[[], str]], ...]
    # Amendment B1: every one of these frames the members' FULL LOGICAL
    # ROWS, so a same-length mutation of a body's bytes, a rewritten
    # `sourceUrl`, or a narrowed evidence coordinate moves the release's
    # name. The rows go in as the bundle carries them; `framed_set_digest`
    # applies the exclusion set, so producer and gate cannot disagree.
    #
    # Amendment C3: `selectedSourceSetDigest` IS here, and B6's reason for
    # leaving it out is withdrawn. The pinned catalog's BYTES are not in the
    # bundle, but the MEMBERS the domain digests are: every disposition row
    # carries both fields `docspec-selected-source-set/1` takes. The member
    # set is every row the CATALOG selected -- which is every row except the
    # two reason codes that say the catalog itself did not, every other
    # refusal in the vocabulary being this producer's about an item the
    # catalog did select. The builder still DERIVES the value from the pin
    # (B6 is unchanged); this is a second, independent route to it from
    # bundle bytes alone, and the two must agree.
    catalog_selected = [
        {"documentId": row.get("documentId"), "sourceItemId": row.get("sourceItemId")}
        for row in dispositions
        if row.get("reasonCode") not in CATALOG_STATE_REASON_CODES
    ]
    digest_plan = (
        (
            "selectedSourceSetDigest",
            lambda: framed_set_digest(SELECTED_SOURCE_SET_DOMAIN, catalog_selected),
        ),
        (
            "sourceDispositionSetDigest",
            lambda: framed_set_digest("docspec-source-disposition-set/3", dispositions),
        ),
        (
            "documentVersionSetDigest",
            lambda: framed_set_digest("docspec-document-version-set/3", documents),
        ),
        (
            "structuralNodeSetDigest",
            lambda: framed_set_digest("docspec-structural-node-set/3", nodes),
        ),
        (
            "segmentSetDigest",
            lambda: framed_set_digest("docspec-segment-set/3", segments),
        ),
        (
            "sourceDocumentMappingDigest",
            lambda: framed_set_digest(SOURCE_TO_DOCUMENT_DOMAIN, joined),
        ),
        (
            "textBodySetDigest",
            lambda: framed_set_digest(TEXT_BODY_SET_DOMAIN, text_bodies),
        ),
        # A release with none of a kind streams the empty set rather than
        # omitting the digest: a zero is written, never omitted.
        (
            "attachmentSetDigest",
            lambda: framed_set_digest("docspec-attachment-set/3", attachments),
        ),
        (
            "commentSetDigest",
            lambda: framed_set_digest("docspec-comment-set/3", comments),
        ),
    )
    for field, compute in digest_plan:
        try:
            expected = compute()
        except (TypeError, ValueError) as exc:
            _issue(issues, "invalid.set-digest", f"release.json/content/{field}", str(exc))
            continue
        if content.get(field) != expected:
            _issue(
                issues,
                "invalid.set-digest",
                f"release.json/content/{field}",
                f"expected {expected}",
            )

    receipt = content.get("joinReceipt")
    if isinstance(receipt, Mapping):
        if receipt.get("mappingDigest") != content.get("sourceDocumentMappingDigest"):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt/mappingDigest",
                "join receipt does not seal the release's mapping digest",
            )
        if receipt.get("selectedSourceItemCount") != len(selected_ids):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt/selectedSourceItemCount",
                f"expected {len(selected_ids)}",
            )
        if receipt.get("documentVersionCount") != len(version_ids):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt/documentVersionCount",
                f"expected {len(version_ids)}",
            )
        if len(selected_ids) != len(version_ids) or len(set(selected_ids)) != len(
            set(version_ids)
        ):
            _issue(
                issues,
                "invalid.join",
                "release.json/content/joinReceipt",
                "the source-to-document join is not one-to-one",
            )

    expected_counts = derive_counts(
        dispositions,
        documents,
        nodes,
        segments,
        member_count=len(members),
        total_member_byte_size=sum(
            member.get("byteSize", 0)
            for member in members
            if isinstance(member.get("byteSize"), int) and not isinstance(member.get("byteSize"), bool)
        ),
        attachments=attachments,
        comments=comments,
    )
    expected_accounting = expected_counts.get("attachmentAccounting")
    declared_counts = content.get("counts")
    declared_accounting = (
        declared_counts.get("attachmentAccounting")
        if isinstance(declared_counts, Mapping)
        else None
    )
    if declared_accounting != expected_accounting:
        _issue(
            issues,
            "invalid.attachment-accounting",
            "release.json/content/counts/attachmentAccounting",
            f"expected {expected_accounting}",
        )
    if content.get("counts") != expected_counts:
        _issue(issues, "invalid.counts", "release.json/content/counts", f"expected {expected_counts}")
    expected_coverage = derive_coverage(
        dispositions,
        documents,
        segments,
        key=key,
        attachments=attachments,
        comments=comments,
    )
    if content.get("coverage") != expected_coverage:
        _issue(
            issues,
            "invalid.coverage",
            "release.json/content/coverage",
            f"expected {expected_coverage}",
        )
    _validate_coverage_identity(content, issues)


def _validate_coverage_identity(
    content: Mapping[str, Any], issues: list[VerificationIssue]
) -> None:
    """``segmented + excluded == representation``, per kind and in aggregate.

    Amendment A2 states the identity twice on purpose. An aggregate that
    balances while one kind's does not is a hole in one kind hidden by a surplus
    in another, and the whole point of the per-kind breakdown is that such a
    hole has nowhere to hide.
    """

    def holds(totals: Any) -> bool:
        values = [
            totals.get(field)
            for field in ("segmentedByteTotal", "excludedByteTotal", "representationByteTotal")
        ]
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return True
        segmented, excluded, representation = values
        return segmented + excluded == representation

    coverage = content.get("coverage")
    if isinstance(coverage, Mapping) and not holds(coverage):
        _issue(
            issues,
            "invalid.coverage",
            "release.json/content/coverage",
            "segmentedByteTotal + excludedByteTotal must equal representationByteTotal",
        )
    counts = content.get("counts")
    per_kind = counts.get("perKind") if isinstance(counts, Mapping) else None
    if not isinstance(per_kind, Mapping):
        return
    for kind in TEXT_KINDS:
        totals = per_kind.get(kind)
        if isinstance(totals, Mapping) and not holds(totals):
            _issue(
                issues,
                "invalid.coverage",
                f"release.json/content/counts/perKind/{kind}",
                "segmentedByteTotal + excludedByteTotal must equal representationByteTotal",
            )
