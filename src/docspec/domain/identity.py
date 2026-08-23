"""Canonical JSON, immutable JSON values, digests, and DocSpec identities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, TypeAlias

from docspec.errors import IntegrityError

JSONScalar: TypeAlias = None | bool | int | str
JSONValue: TypeAlias = JSONScalar | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]
JSONObject: TypeAlias = Mapping[str, JSONValue]

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_URN_PART_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def require_text(value: object, label: str) -> str:
    """Return one non-empty string or fail with a stable message."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def require_sha256(value: object, label: str = "digest") -> str:
    """Return one normalized ``sha256:`` digest."""

    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256 followed by 64 lowercase hexadecimal characters")
    return value


def require_relative_path(value: object, label: str = "path") -> str:
    """Return a safe portable relative path."""

    text = require_text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a contained relative path")
    return path.as_posix()


def closed_mapping(
    value: object,
    keys: Iterable[str],
    label: str,
    *,
    error: type[Exception] = IntegrityError,
) -> Mapping[str, Any]:
    """Return one mapping whose keys are exactly ``keys``, or refuse it.

    ``error`` names the boundary, not a second rule. A domain value object
    reading its own dict raises ``ValueError``; bytes admitted from outside fail
    closed with ``IntegrityError``; a profile raises ``ProfileError``. The check
    those boundaries share -- a mapping, and exactly these keys, no more and no
    fewer -- is written once, here.
    """

    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise error(f"{label} has an invalid closed shape")
    return value


def freeze_json(value: Any, *, label: str = "value") -> JSONValue:
    """Return an immutable JSON value and reject ambiguous inputs."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Enum):
        return freeze_json(value.value, label=label)
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        raise ValueError(f"{label} contains a floating-point number; use an integer unit or decimal string")
    if isinstance(value, Mapping):
        frozen: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-string object key")
            if key in frozen:
                raise ValueError(f"{label} contains a duplicate key: {key}")
            frozen[key] = freeze_json(item, label=f"{label}.{key}")
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return tuple(freeze_json(item, label=f"{label}[]") for item in value)
    raise ValueError(f"{label} contains unsupported type {type(value).__name__}")


def thaw_json(value: JSONValue) -> Any:
    """Return a mutable JSON-shaped copy suitable for standard encoders."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one value with DocSpec's identity-bearing JSON rules."""

    plain = thaw_json(freeze_json(value))
    return json.dumps(plain, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_json_file_bytes(value: Any) -> bytes:
    """Encode a canonical JSON file with one trailing newline."""

    return canonical_json_bytes(value) + b"\n"


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def parse_canonical_json(data: bytes, *, label: str = "JSON", file_form: bool = True) -> JSONValue:
    """Parse exact canonical UTF-8 JSON and reject alternate encodings."""

    value = parse_closed_json(data, label=label)
    expected = canonical_json_file_bytes(value) if file_form else canonical_json_bytes(value)
    if data != expected:
        raise IntegrityError(f"{label} is not canonical JSON")
    return value


def parse_closed_json(data: bytes, *, label: str = "JSON") -> JSONValue:
    """Parse duplicate-safe finite UTF-8 JSON without imposing file formatting."""

    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_closed_object, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise IntegrityError(f"{label} is not valid closed UTF-8 JSON: {error}") from error
    return freeze_json(value, label=label)


def sha256_digest(data: bytes) -> str:
    """Return the normalized SHA-256 digest of exact bytes."""

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def identity_digest(value: Any) -> str:
    """Digest one canonical identity-bearing value."""

    return sha256_digest(canonical_json_bytes(value))


class OrderedJsonSequenceDigester:
    """Incrementally digest one canonical JSON array with a single framing implementation."""

    __slots__ = ("_digest", "_finished", "_first", "_result")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._first = True
        self._finished = False
        self._result: str | None = None

    def accept(self, value: Any) -> None:
        if self._finished:
            raise RuntimeError("ordered JSON sequence digest is already complete")
        if not self._first:
            self._digest.update(b",")
        self._digest.update(canonical_json_bytes(value))
        self._first = False

    def finish(self) -> str:
        if not self._finished:
            self._digest.update(b"]")
            self._result = f"sha256:{self._digest.hexdigest()}"
            self._finished = True
        assert self._result is not None
        return self._result


def ordered_json_sequence_digest(values: Iterable[Any]) -> str:
    """Digest a canonical JSON array without retaining all items in memory."""

    digest = OrderedJsonSequenceDigester()
    for value in values:
        digest.accept(value)
    return digest.finish()


def stable_urn(kind: str, value: Any, *, version: int = 1) -> str:
    """Create a content-derived DocSpec URN."""

    kind = require_text(kind, "identity kind")
    if _URN_PART_RE.fullmatch(kind) is None:
        raise ValueError("identity kind must use lowercase letters, digits, and hyphens")
    digest = identity_digest(value).removeprefix("sha256:")
    return f"urn:docspec:{kind}:v{version}:{digest}"
