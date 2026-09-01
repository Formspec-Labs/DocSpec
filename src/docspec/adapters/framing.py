"""Framed digests and canonical record bytes, fast where equality is proven.

``FramedSectionHasher`` restates Rulespec's ``framed_section_digest`` byte
protocol incrementally so many digests can share one pass over their rows;
``framed_section_digest_fast`` is a drop-in for the batch function over any
number of sections. ``canonical_record_payload`` serializes a record through
std-json when a structural guard proves byte-equality with the Rulespec writer
(ASCII object keys, no floats) and falls back to that writer otherwise. Both
equalities are pinned by test; DocSpec's catalog minter runs on them.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterable, Mapping

from rulespec_artifacts import FramedSection, canonical_json_bytes

from docspec.errors import IntegrityError


def is_fast_canonical_safe(value: object) -> bool:
    """True when std-json canonical output provably equals Rulespec's.

    With every object key ASCII and no float anywhere, ``json.dumps`` with
    sorted keys, no-ASCII escaping disabled, and compact separators emits the
    same bytes as the Rulespec canonical writer: string escaping is identical
    (Rulespec delegates each string to ``json.dumps``), the separators match,
    and for ASCII keys code-point order equals the UTF-16 order Rulespec sorts
    by. Anything outside that domain falls back to the Rulespec writer itself.
    """

    stack = [value]
    while stack:
        current = stack.pop()
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str or not key.isascii():
                    return False
                stack.append(item)
        elif type(current) is list:
            stack.extend(current)
        elif type(current) is float:
            return False
    return True


def canonical_record_payload(record: Mapping[str, object]) -> bytes:
    """Canonical bytes for one framed record, fast where equality is proven."""

    if is_fast_canonical_safe(record):
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    return canonical_json_bytes(record)


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
        self.add_payload(canonical_record_payload(record))

    def digest(self) -> str:
        if self._observed != self._count:
            raise IntegrityError(
                f"cannot compute {self._domain}: section {self._name!r} declared "
                f"{self._count} records but yielded {self._observed}"
            )
        return "sha256:" + self._digest.hexdigest()


def framed_section_digest_fast(domain: str, sections: Iterable[FramedSection]) -> str:
    """Rulespec's ``framed_section_digest``, computed with the fast record writer."""

    if not isinstance(domain, str) or not domain:
        raise IntegrityError("digest domain must be nonempty text")
    digest = hashlib.sha256(domain.encode("utf-8") + b"\0")
    names: set[str] = set()
    for section in sections:
        if not isinstance(section, FramedSection):
            raise IntegrityError("sections must contain FramedSection values")
        if not section.name or section.name in names:
            raise IntegrityError("section names must be nonempty and distinct")
        names.add(section.name)
        name_bytes = section.name.encode("utf-8")
        digest.update(struct.pack(">Q", len(name_bytes)))
        digest.update(name_bytes)
        digest.update(struct.pack(">Q", section.count))
        observed = 0
        for record in section.records:
            payload = canonical_record_payload(record)
            digest.update(struct.pack(">Q", len(payload)))
            digest.update(payload)
            observed += 1
            if observed > section.count:
                raise IntegrityError(f"section {section.name!r} exceeds its declared count")
        if observed != section.count:
            raise IntegrityError(
                f"section {section.name!r} declared {section.count} records but yielded {observed}"
            )
    return "sha256:" + digest.hexdigest()


__all__ = [
    "FramedSectionHasher",
    "canonical_record_payload",
    "framed_section_digest_fast",
    "is_fast_canonical_safe",
]
