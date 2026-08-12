"""Structural conformance of an exchanged SourceCatalogRelease against its pinned schemas."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.domain.identity import (
    parse_canonical_json,
    parse_closed_json,
    require_relative_path,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError
from docspec.ports.source_release import SourceReleaseConformance, SourceReleaseViolation

WIRE_FORMAT = "spicy-regs-source-catalog-release"
WIRE_FORMAT_VERSION = "1.0"
WIRE_ROOT_MEMBER = "release.json"
WIRE_MANIFEST_MEMBER = "manifests/global.json"
WIRE_ITEMS_MEMBER = "data/source-items.json"
WIRE_SCHEMA_ROLES = ("release-root", "member-manifest", "source-items")

PINS_FORMAT = "docspec-wire-release-pins"
PINS_FORMAT_VERSION = "1.0"
PINS_IDENTITY_KIND = "wire-release-pins"
MAX_PINS_BYTES = 8 * 1024**2
MAX_WIRE_MEMBER_BYTES = 64 * 1024**2

_PINS_KEYS = frozenset({"bundles", "format", "formatVersion", "members", "origin", "pinsId", "release", "schemas"})
_ORIGIN_KEYS = frozenset({"candidateId", "candidateStatus", "candidateVersion", "recordType"})
_RELEASE_KEYS = frozenset({"name", "version", "wireFormat", "wireFormatVersion"})
_MEMBER_KEYS = frozenset({"byteSize", "digest", "mediaType", "path"})
_SCHEMA_KEYS = frozenset({"originPath", "path", "role", "schemaId"})
_BUNDLE_KEYS = frozenset({"name", "originPath", "path", "releaseId", "structuralVerdict", "upstreamCode", "upstreamPath"})
_STRUCTURAL_VERDICTS = frozenset({"conforms", "violates"})
_SCHEMA_MEDIA_TYPE = "application/schema+json"


class WireSourceReleaseError(DocSpecError):
    """The wire source-release gate could not be built or applied."""


@dataclass(frozen=True, slots=True)
class WireSourceReleaseBundle:
    """The three members of one exchanged release that carry structural shape."""

    root: Mapping[str, Any]
    manifest: Mapping[str, Any]
    items: Sequence[Any]


@dataclass(frozen=True, slots=True)
class WireReleaseBundlePin:
    """One pinned conformance bundle and the verdict its bytes must produce."""

    name: str
    directory: Path
    release_id: str
    conforms: bool
    upstream_code: str
    upstream_path: str | None


@dataclass(frozen=True, slots=True)
class WireReleasePins:
    """Every pinned byte of one wire release's schema set and conformance bundles."""

    pins_id: str
    candidate_id: str
    wire_format: str
    wire_format_version: str
    schemas: Mapping[str, Any]
    bundles: tuple[WireReleaseBundlePin, ...]


def _closed_mapping(value: object, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise IntegrityError(f"{label} has an invalid closed shape")
    return value


def _count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IntegrityError(f"{label} must be a non-negative integer")
    return value


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"{label} must be a regular, non-symlink file: {path}")
    return path


def _read_bounded(path: Path, *, label: str, max_bytes: int) -> bytes:
    _regular_file(path, label)
    if path.stat().st_size > max_bytes:
        raise LimitExceededError(f"{label} exceeds the {max_bytes}-byte limit")
    return path.read_bytes()


def _tracked_files(directory: Path) -> set[str]:
    names: set[str] = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise IntegrityError(f"pinned wire release tree contains a symlink: {path}")
        if path.is_file():
            names.add(path.relative_to(directory).as_posix())
    return names


def _verified_members(directory: Path, declared: Iterable[Any]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for member in declared:
        member = _closed_mapping(member, _MEMBER_KEYS, "pinned wire release member")
        relative = require_relative_path(member["path"], "pinned wire release member path")
        if relative in payloads:
            raise IntegrityError(f"pinned wire release member is declared twice: {relative}")
        digest = require_sha256(member["digest"], "pinned wire release member digest")
        byte_size = _count(member["byteSize"], "pinned wire release member byte size")
        require_text(member["mediaType"], "pinned wire release member media type")
        path = _regular_file(directory / relative, f"pinned wire release member {relative}")
        if path.stat().st_size != byte_size:
            raise IntegrityError(f"pinned wire release member differs in size from its pin: {relative}")
        payload = path.read_bytes()
        if sha256_digest(payload) != digest:
            raise IntegrityError(f"pinned wire release member differs from its pinned digest: {relative}")
        payloads[relative] = payload
    return payloads


def load_wire_release_pins(path: Path) -> WireReleasePins:
    """Admit one pins file and every byte it pins, refusing any digest mismatch.

    The pins file is the only thing named by path. Everything else is resolved
    as a contained relative path below it, size-checked, and re-digested before
    a parser sees it, so a rewritten schema or fixture fails here rather than
    changing a verdict somewhere downstream.
    """

    pins_path = _regular_file(Path(path).resolve(strict=True), "wire release pins file")
    directory = pins_path.parent
    payload = _read_bounded(pins_path, label="wire release pins file", max_bytes=MAX_PINS_BYTES)
    document = thaw_json(parse_canonical_json(payload, label="wire release pins file"))
    pins = _closed_mapping(document, _PINS_KEYS, "wire release pins file")
    if pins["format"] != PINS_FORMAT or pins["formatVersion"] != PINS_FORMAT_VERSION:
        raise IntegrityError("wire release pins file has an unknown format")
    content = {name: value for name, value in pins.items() if name != "pinsId"}
    if pins["pinsId"] != stable_urn(PINS_IDENTITY_KIND, content):
        raise IntegrityError("wire release pins identity differs from its canonical content")

    origin = _closed_mapping(pins["origin"], _ORIGIN_KEYS, "wire release pins origin")
    release = _closed_mapping(pins["release"], _RELEASE_KEYS, "wire release pins release")
    if release["wireFormat"] != WIRE_FORMAT or release["wireFormatVersion"] != WIRE_FORMAT_VERSION:
        raise IntegrityError("wire release pins name a format this gate does not check")

    declared = pins["members"]
    if not isinstance(declared, list) or not declared:
        raise IntegrityError("wire release pins must declare at least one member")
    payloads = _verified_members(directory, declared)
    if _tracked_files(directory) - {pins_path.name} != set(payloads):
        raise IntegrityError("pinned wire release tree has missing or extra files")

    schemas: dict[str, Any] = {}
    for entry in pins["schemas"]:
        entry = _closed_mapping(entry, _SCHEMA_KEYS, "pinned wire release schema")
        role = require_text(entry["role"], "pinned wire release schema role")
        if role not in WIRE_SCHEMA_ROLES or role in schemas:
            raise IntegrityError(f"pinned wire release schema role is unknown or repeated: {role}")
        require_text(entry["schemaId"], "pinned wire release schema id")
        relative = require_relative_path(entry["path"], "pinned wire release schema path")
        if relative not in payloads:
            raise IntegrityError(f"pinned wire release schema is not a pinned member: {relative}")
        schemas[role] = thaw_json(parse_closed_json(payloads[relative], label=f"pinned wire release schema {relative}"))
    if set(schemas) != set(WIRE_SCHEMA_ROLES):
        raise IntegrityError("wire release pins do not pin one schema for every role")

    bundles: list[WireReleaseBundlePin] = []
    for entry in pins["bundles"]:
        entry = _closed_mapping(entry, _BUNDLE_KEYS, "pinned wire release bundle")
        verdict = entry["structuralVerdict"]
        if verdict not in _STRUCTURAL_VERDICTS:
            raise IntegrityError(f"pinned wire release bundle verdict is unknown: {verdict}")
        relative = require_relative_path(entry["path"], "pinned wire release bundle path")
        upstream_path = entry["upstreamPath"]
        if upstream_path is not None:
            require_text(upstream_path, "pinned wire release bundle upstream path")
        bundles.append(
            WireReleaseBundlePin(
                require_text(entry["name"], "pinned wire release bundle name"),
                directory / relative,
                require_text(entry["releaseId"], "pinned wire release bundle release id"),
                verdict == "conforms",
                require_text(entry["upstreamCode"], "pinned wire release bundle upstream code"),
                upstream_path,
            )
        )
    if not bundles:
        raise IntegrityError("wire release pins must pin at least one conformance bundle")

    return WireReleasePins(
        pins["pinsId"],
        require_text(origin["candidateId"], "wire release pins candidate id"),
        release["wireFormat"],
        release["wireFormatVersion"],
        schemas,
        tuple(bundles),
    )


def read_wire_release_bundle(directory: Path, *, max_member_bytes: int = MAX_WIRE_MEMBER_BYTES) -> WireSourceReleaseBundle:
    """Read the three shape-bearing members of one exchanged release bundle.

    A member that is missing, oversized, or not duplicate-safe UTF-8 JSON is
    refused here. Whether the bundle's declared membership is complete is the
    publisher's rule, not this reader's, and is not decided.
    """

    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise IntegrityError(f"wire source release bundle must be a directory: {root}")
    documents: list[Any] = []
    for member in (WIRE_ROOT_MEMBER, WIRE_MANIFEST_MEMBER, WIRE_ITEMS_MEMBER):
        path = root.joinpath(*require_relative_path(member, "wire source release member").split("/"))
        payload = _read_bounded(path, label=f"wire source release member {member}", max_bytes=max_member_bytes)
        documents.append(thaw_json(parse_closed_json(payload, label=f"wire source release member {member}")))
    release_root, manifest, items = documents
    if not isinstance(release_root, Mapping) or not isinstance(manifest, Mapping):
        raise IntegrityError("wire source release root and member manifest must be JSON objects")
    if not isinstance(items, list):
        raise IntegrityError("wire source release source items must be a JSON array")
    return WireSourceReleaseBundle(release_root, manifest, items)


def _pointer(parts: Iterable[Any]) -> str:
    return "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in parts)


class JsonSchemaWireSourceReleaseGate:
    """Check one exchanged SourceCatalogRelease against its pinned Draft 2020-12 schemas.

    This is the consumer-side structural gate: it decides only the shape the
    published JSON Schema set decides, over the release root, the global member
    manifest, and each source item. Contract authority is Rulespec's own
    validator, at the cross-product verdict-agreement step (SpicySearch PLAN
    step 7); this gate does not claim its diagnostic codes, and it decides none
    of the identity, membership, digest, count, coverage, path, duplicate, or
    canonical-encoding rules that validator also decides. A bundle this gate
    reports as conforming has passed structure and nothing else.
    """

    def __init__(self, schemas: Mapping[str, Any]) -> None:
        if set(schemas) != set(WIRE_SCHEMA_ROLES):
            raise ValueError(f"a wire source release gate needs one schema per role: {WIRE_SCHEMA_ROLES}")
        try:
            from jsonschema import Draft202012Validator  # type: ignore[import-not-found]
        except ImportError as error:
            raise WireSourceReleaseError("install the docspec[wire] extra to check a wire-format source release") from error
        validators = {}
        for role, document in schemas.items():
            try:
                Draft202012Validator.check_schema(document)
            except Exception as error:  # noqa: BLE001 - the validator raises its own error type
                raise WireSourceReleaseError(f"pinned wire source release schema is not valid: {role}") from error
            validators[role] = Draft202012Validator(document)
        self._validators = validators

    @classmethod
    def from_pins(cls, pins: WireReleasePins) -> JsonSchemaWireSourceReleaseGate:
        return cls(pins.schemas)

    def _violations(self, member: str, role: str, value: Any, *, prefix: str = "") -> list[SourceReleaseViolation]:
        errors = sorted(
            self._validators[role].iter_errors(value),
            key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
        )
        return [SourceReleaseViolation(member, prefix + _pointer(error.absolute_path), error.message) for error in errors]

    def check(
        self,
        *,
        root: Mapping[str, Any],
        manifest: Mapping[str, Any],
        items: Sequence[Any],
    ) -> SourceReleaseConformance:
        """Return every root and manifest violation, and the first violation of each item."""

        violations = [
            *self._violations(WIRE_ROOT_MEMBER, "release-root", root),
            *self._violations(WIRE_MANIFEST_MEMBER, "member-manifest", manifest),
        ]
        for index, record in enumerate(items):
            violations.extend(self._violations(WIRE_ITEMS_MEMBER, "source-items", record, prefix=f"/{index}")[:1])
        return SourceReleaseConformance(tuple(violations))

    def check_bundle(self, directory: Path, *, max_member_bytes: int = MAX_WIRE_MEMBER_BYTES) -> SourceReleaseConformance:
        """Read one exchanged release bundle from disk and return its structural verdict."""

        bundle = read_wire_release_bundle(directory, max_member_bytes=max_member_bytes)
        return self.check(root=bundle.root, manifest=bundle.manifest, items=bundle.items)


__all__ = [
    "JsonSchemaWireSourceReleaseGate",
    "MAX_WIRE_MEMBER_BYTES",
    "WIRE_FORMAT",
    "WIRE_FORMAT_VERSION",
    "WIRE_ITEMS_MEMBER",
    "WIRE_MANIFEST_MEMBER",
    "WIRE_ROOT_MEMBER",
    "WIRE_SCHEMA_ROLES",
    "WireReleaseBundlePin",
    "WireReleasePins",
    "WireSourceReleaseBundle",
    "WireSourceReleaseError",
    "load_wire_release_pins",
    "read_wire_release_bundle",
]
