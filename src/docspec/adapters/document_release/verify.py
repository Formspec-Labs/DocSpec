"""Coordinate portable release and sealed corpus verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from docspec.adapters.document_release.bindings import _validate_root_bindings
from docspec.adapters.document_release.body_index import _read_text_body_index
from docspec.adapters.document_release.diagnostics import VerificationIssue, VerificationResult
from docspec.adapters.document_release.members import (
    _read_member_manifest,
    _read_root,
    _read_rows,
    _verify_member_files,
)
from docspec.adapters.document_release.processing import (
    _validate_coverage,
    _validate_processing_policies,
    _validate_segments,
    _validate_structure,
)
from docspec.adapters.document_release.rows import (
    _validate_attachments,
    _validate_comments,
    _validate_dispositions,
    _validate_documents,
)
from docspec.adapters.document_release.rules import TEXT_BODY_KEY
from docspec.adapters.document_release.schemas import _validate_root_shape, _validate_schema_set
from docspec.document_release_support import (
    tree_digest,
)


def verify_document_release(bundle: Path) -> VerificationResult:
    """Verify one materialized ``DocumentRelease`` v2 bundle.

    Nothing an untrusted bundle contains can make this raise (amendment B3). A
    gate that can be crashed is a gate that can be skipped, so an escaping
    exception is caught here and reported as the refusal it is, naming the
    bundle. `_verify_document_release` holds the rules; this holds the promise
    that they always produce a verdict.
    """

    try:
        return _verify_document_release(Path(bundle))
    except Exception as exc:  # noqa: BLE001 - the gate must always answer
        return VerificationResult(
            None,
            (
                VerificationIssue(
                    code="invalid.root-syntax",
                    path="release.json",
                    message=f"the bundle could not be read: {type(exc).__name__}: {exc}",
                ),
            ),
        )


def _verify_document_release(bundle: Path) -> VerificationResult:
    """Verify one materialized ``DocumentRelease`` v2 bundle."""

    bundle = Path(bundle)
    issues: list[VerificationIssue] = []
    root = _read_root(bundle, issues)
    if root is None:
        return VerificationResult(None, tuple(issues))
    key = TEXT_BODY_KEY
    members, member_paths, declared = _read_member_manifest(bundle, root, issues)
    _verify_member_files(bundle, members, member_paths, declared, issues)
    schemas = _validate_schema_set(root, members, member_paths, issues)
    _validate_root_shape(root, schemas, issues)

    def rows(role: str) -> tuple[list[dict[str, Any]] | None, str]:
        return _read_rows(role, members, member_paths, schemas, issues)

    dispositions, dispositions_key = rows("source-dispositions")
    documents, documents_key = rows("documents")
    nodes, nodes_key = rows("structural-nodes")
    segments, segments_key = rows("search-segments")
    attachments, attachments_key = rows("attachments")
    comments, comments_key = rows("comments")
    slices = _read_text_body_index(members, member_paths, schemas, issues)

    if dispositions is not None:
        _validate_dispositions(dispositions, dispositions_key, issues)
    sizes: dict[str, int] = {}
    if documents is not None and dispositions is not None:
        sizes = _validate_documents(
            documents,
            dispositions,
            member_paths,
            slices,
            documents_key,
            key,
            issues,
        )
    if comments is not None and documents is not None:
        sizes |= _validate_comments(
            comments, documents, member_paths, slices, comments_key, issues
        )
    if attachments is not None and documents is not None and comments is not None:
        owners = {
            **{
                document[key]: "document-body"
                for document in documents
                if isinstance(document.get(key), str)
            },
            **{
                comment["textBodyId"]: "comment"
                for comment in comments
                if isinstance(comment.get("textBodyId"), str)
            },
        }
        sizes |= _validate_attachments(
            attachments, owners, member_paths, slices, attachments_key, issues
        )
    node_index: dict[str, dict[str, Any]] = {}
    if nodes is not None:
        node_index = _validate_structure(nodes, sizes, nodes_key, key, issues)
    if segments is not None and documents is not None:
        # Evidence is checked against the captured bytes of the body the segment
        # names, whichever kind that body is: one text pipeline, three kinds.
        renditions: dict[Any, Mapping[str, Any]] = {}
        for row, body_key in (
            *((document, key) for document in documents),
            *((comment, "textBodyId") for comment in comments or []),
        ):
            if isinstance(row.get("capture"), Mapping):
                renditions[row.get(body_key)] = row["capture"]
        for attachment in attachments or []:
            body_id = attachment.get("textBodyId")
            for rendition in attachment.get("renditions") or []:
                if (
                    isinstance(rendition, Mapping)
                    and rendition.get("attachmentDisposition") == "text-captured"
                    and isinstance(rendition.get("capture"), Mapping)
                ):
                    renditions[body_id] = rendition["capture"]
        _validate_segments(segments, node_index, renditions, sizes, segments_key, key, issues)
        _validate_coverage(documents, segments, sizes, documents_key, key, issues)
    if (
        documents is not None
        and attachments is not None
        and comments is not None
    ):
        content = root.get("content")
        if isinstance(content, Mapping):
            _validate_processing_policies(
                content, documents, attachments, comments, issues
            )
    if None not in (dispositions, documents, attachments, comments, nodes, segments):
        _validate_root_bindings(
            root,
            dispositions,
            documents,
            attachments,
            comments,
            nodes,
            segments,
            members,
            documents_key,
            key,
            issues,
        )

    release_id = root.get("releaseId")
    return VerificationResult(
        release_id if isinstance(release_id, str) else None, tuple(issues)
    )


def verify_corpus(corpus_file: Path) -> list[dict[str, Any]]:
    """Verify every sealed fixture and return one row per case.

    The corpus path is a required argument: the fixture root is a test input,
    not a packaged one, so an installed DocSpec cannot name a default for it.
    """

    corpus = json.loads(Path(corpus_file).read_text(encoding="utf-8"))
    fixture_root = Path(corpus_file).parent
    rows: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        bundle = fixture_root / case["bundle"]
        observed_tree = tree_digest(bundle) if bundle.is_dir() else None
        result = (
            verify_document_release(bundle) if bundle.is_dir() else VerificationResult(None, ())
        )
        rows.append(
            {
                "name": case["name"],
                "bundle": case["bundle"],
                "sealed": observed_tree == case["treeSha256"],
                "expectedCode": case["expectedCode"],
                "observedCode": result.code if bundle.is_dir() else "absent",
                "expectedPath": case["expectedPath"],
                "observedPath": result.path if bundle.is_dir() else None,
                "issues": [str(issue) for issue in result.issues],
            }
        )
    return rows
