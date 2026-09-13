"""Incremental Rulespec framing for several digests over one record stream."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping

from rulespec_artifacts import canonical_json_bytes

from docspec.errors import IntegrityError


class FramedSectionHasher:
    """Incrementally reproduce ``framed_section_digest`` for one known-count section.

    Byte-for-byte the same protocol Rulespec's batch function seals -- domain,
    NUL, u64 name length, name, u64 count, then u64 payload length + payload per
    record -- so many digests can share one pass over the rows instead of each
    demanding its own. Equality with the batch function is pinned by test.
    """

    __slots__ = ("_digest", "_domain", "_name", "_count", "_observed")

    def __init__(self, domain: str, name: str, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise IntegrityError(f"cannot compute {domain}: section count must be a non-negative integer")
        self._digest = hashlib.sha256(domain.encode("utf-8") + b"\0")
        name_bytes = name.encode("utf-8")
        self._digest.update(struct.pack(">Q", len(name_bytes)))
        self._digest.update(name_bytes)
        self._digest.update(struct.pack(">Q", count))
        self._domain = domain
        self._name = name
        self._count = count
        self._observed = 0

    def add_payload(self, payload: bytes) -> None:
        self._observed += 1
        if self._observed > self._count:
            raise IntegrityError(
                f"cannot compute {self._domain}: section {self._name!r} exceeds its declared count"
            )
        self._digest.update(struct.pack(">Q", len(payload)))
        self._digest.update(payload)

    def add(self, record: Mapping[str, object]) -> None:
        self.add_payload(canonical_json_bytes(record))

    def digest(self) -> str:
        if self._observed != self._count:
            raise IntegrityError(
                f"cannot compute {self._domain}: section {self._name!r} declared "
                f"{self._count} records but yielded {self._observed}"
            )
        return "sha256:" + self._digest.hexdigest()


__all__ = ["FramedSectionHasher"]
