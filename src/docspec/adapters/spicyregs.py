"""The only runtime import boundary from DocSpec into SpicyRegs."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pypdf import PdfReader

from spicy_regs.data_dictionary import expected_schemas
from spicy_regs.pipelines.base import Pipeline
from spicy_regs.pipelines.rollups.base import RollupPipeline, make_rollup_app
from spicy_regs.schemas import DOCUMENT
from spicy_regs.sources.base import Reader
from spicy_regs.sources.mirrulations import BUCKET, PREFIX, s3_client, s3_resource
from spicy_regs.transforms.pdf_text import (
    PAGE_SEPARATOR,
    PdfTextResult as SpicyRegsPdfTextResult,
    PdfTextStatus,
    extract_pdf_text as extract_spicyregs_pdf_text,
)

@dataclass(frozen=True)
class PdfTextResult(SpicyRegsPdfTextResult):
    """The SpicyRegs result extended with DocSpec's page-level evidence."""

    pages: tuple[str, ...] = ()
    failed_page_ordinals: tuple[int, ...] = ()


def extract_pdf_text(
    data: bytes,
    *,
    page_separator: str = PAGE_SEPARATOR,
    page_whitespace: Literal["preserve", "strip"] = "strip",
) -> PdfTextResult:
    """Adapt the pinned SpicyRegs extractor to DocSpec's page-level interface."""

    if not isinstance(page_separator, str) or not page_separator:
        raise ValueError("page_separator must be a non-empty string")
    if page_whitespace not in {"preserve", "strip"}:
        raise ValueError("page_whitespace must be 'preserve' or 'strip'")

    upstream = extract_spicyregs_pdf_text(data)
    if upstream.status not in {PdfTextStatus.OK, PdfTextStatus.EMPTY}:
        return PdfTextResult(
            status=upstream.status,
            text=upstream.text,
            page_count=upstream.page_count,
            error=upstream.error,
        )

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    failed_page_ordinals: list[int] = []
    for ordinal, page in enumerate(reader.pages, start=1):
        try:
            part = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - record one unreadable page without losing the document
            part = ""
            failed_page_ordinals.append(ordinal)
        parts.append(part.strip() if page_whitespace == "strip" else part)

    pages = tuple(parts)
    text = page_separator.join(pages)
    if page_whitespace == "strip":
        text = text.strip()
    status = PdfTextStatus.OK if text else PdfTextStatus.EMPTY
    return PdfTextResult(
        status=status,
        text=text,
        page_count=len(pages),
        pages=pages,
        failed_page_ordinals=tuple(failed_page_ordinals),
    )


@dataclass(frozen=True, slots=True)
class DownloadedObject:
    content: bytes
    etag: str | None
    last_modified: datetime | None
    content_length: int | None


def download_object_bytes(
    resource: Any,
    bucket_name: str,
    key: str,
    *,
    if_match: str | None = None,
    max_bytes: int | None = None,
) -> DownloadedObject:
    """Fetch exact object bytes while enforcing the caller's size and ETag pins."""

    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    response = resource.Object(bucket_name, key).get(
        **({"IfMatch": if_match} if if_match is not None else {})
    )
    declared_length = response.get("ContentLength")
    if max_bytes is not None and declared_length is not None and declared_length > max_bytes:
        raise ValueError(f"{key} exceeds the {max_bytes} byte cap")
    body = response["Body"]
    try:
        content = body.read(max_bytes + 1) if max_bytes is not None else body.read()
    finally:
        body.close()
    if max_bytes is not None and len(content) > max_bytes:
        raise ValueError(f"{key} exceeds the {max_bytes} byte cap")
    if declared_length is not None and declared_length != len(content):
        raise ValueError(f"{key} returned {len(content)} bytes but declared {declared_length}")
    etag = response.get("ETag")
    if if_match is not None and etag != if_match:
        raise ValueError(f"{key} returned ETag {etag!r}, expected {if_match!r}")
    return DownloadedObject(
        content=content,
        etag=etag,
        last_modified=response.get("LastModified"),
        content_length=declared_length,
    )


__all__ = [
    "BUCKET",
    "DOCUMENT",
    "PAGE_SEPARATOR",
    "PdfTextResult",
    "PdfTextStatus",
    "PREFIX",
    "Pipeline",
    "Reader",
    "RollupPipeline",
    "download_object_bytes",
    "expected_schemas",
    "extract_pdf_text",
    "make_rollup_app",
    "s3_client",
    "s3_resource",
]
