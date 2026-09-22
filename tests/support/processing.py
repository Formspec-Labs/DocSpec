"""Shared processing fixtures, extracted from tests.test_processing_pipeline."""

from __future__ import annotations

from docspec.domain.content import CapturedFile
from docspec.domain.identity import sha256_digest
from docspec.domain.references import BlobRef


def _captured(content: bytes, media_type: str) -> CapturedFile:
    """Build the fixture captured-file record for ``content`` with a digest-addressed locator."""
    blob = BlobRef(
        locator=f"fixture://{sha256_digest(content).removeprefix('sha256:')}",
        digest=sha256_digest(content),
        byte_size=len(content),
        media_type=media_type,
    )
    return CapturedFile.create(
        source_item_id="source:item-1",
        source_version="2026-08-05",
        candidate_id="primary",
        blob=blob,
        media_type=media_type,
        acquired_at="2026-08-05T12:01:00Z",
        downloader_id="fixture-downloader/v1",
        transport_version="fixture-v1",
        acquisition_started_at="2026-08-05T12:00:00Z",
        downloader_configuration_digest=sha256_digest(b"fixture-downloader-config"),
        task_id="fixture-task",
        attempt_id="fixture-attempt",
    )
