"""Temporary storage adapter backed by SpicyRegs' published object store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spicy_regs.sources import r2

from docspec.ports.document_store import DocumentStore


class SpicyRegsDocumentStore(DocumentStore):
    """Use the existing object store without giving it DocSpec ownership."""

    def fetch(self, object_key: str, local_path: Path) -> bool:
        return r2.download(object_key, local_path)

    def publish(self, local_path: Path, *, object_key: str, **options: Any) -> None:
        if options:
            unsupported = ", ".join(sorted(options))
            raise RuntimeError(
                "The pinned SpicyRegs storage adapter does not support DocSpec "
                f"publication options: {unsupported}"
            )
        r2.upload_file(local_path, remote_key=object_key)


document_store = SpicyRegsDocumentStore()
