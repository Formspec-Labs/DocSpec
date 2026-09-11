"""Open the exact catalog payload bytes identified by admitted descriptors."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from tempfile import TemporaryFile
from typing import BinaryIO

from rulespec_artifacts import ArtifactVerificationError, LocalBlobSource

from docspec.adapters.catalog_artifact.rules import _CatalogPartition
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import SourceCatalogBlobSource


class _CatalogPayloadSource:
    def __init__(self, source: SourceCatalogBlobSource, partitions: Sequence[_CatalogPartition]) -> None:
        self._source = source
        self._sizes = {partition.member.blob_ref: partition.member.byte_size for partition in partitions}

    @contextmanager
    def open(self, blob_ref: str) -> Iterator[BinaryIO]:
        expected_size = self._sizes[blob_ref]
        try:
            if isinstance(self._source, LocalBlobSource):
                with self._source.open(blob_ref) as stream:
                    # Rulespec checks the digest before yielding and detects
                    # replacement or mutation while the same file is open.
                    if stream.seek(0, 2) != expected_size:
                        raise IntegrityError("source catalog payload size differs from its descriptor")
                    stream.seek(0)
                    yield stream
                return
            # An arbitrary provider need not offer a seekable stream or a
            # local immutable file. Retain and hash this exact read before
            # exposing rows, with constant memory and descriptor-bounded IO.
            with TemporaryFile(mode="w+b") as retained:
                with self._source.open(blob_ref) as stream:
                    digest = hashlib.sha256()
                    size = 0
                    while block := stream.read(min(256 * 1024, expected_size - size + 1)):
                        size += len(block)
                        if size > expected_size:
                            raise IntegrityError("source catalog payload size differs from its descriptor")
                        digest.update(block)
                        retained.write(block)
                    if size != expected_size or "sha256:" + digest.hexdigest() != blob_ref:
                        raise IntegrityError("source catalog payload differs from its content address")
                # The retained bytes are sufficient now. Release the provider
                # before merging rows from this and the other partitions.
                retained.seek(0)
                yield retained
        except ArtifactVerificationError as error:
            raise IntegrityError(f"source catalog payload is invalid: {error}") from error
