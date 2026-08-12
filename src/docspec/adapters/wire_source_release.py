"""Structural conformance of an exchanged SourceCatalogRelease against its pinned schemas."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
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
from docspec.domain.references import SourceCatalogRef
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError
from docspec.ports.source_catalog import SourceCatalogSummary
from docspec.ports.source_release import (
    SourceReleaseAdmission,
    SourceReleaseConformance,
    SourceReleasePin,
    SourceReleaseRead,
    SourceReleaseSchemaGate,
    SourceReleaseViolation,
)

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


class _DuplicateSafeDict(dict[str, Any]):
    """Mapping factory that makes ijson refuse duplicate object keys."""

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self:
            raise IntegrityError(f"wire source release JSON contains duplicate key {key!r}")
        super().__setitem__(key, value)


class _DigestingReader:
    """Count and digest the exact bytes ijson consumes."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._digest = hashlib.sha256()
        self.byte_count = 0

    def read(self, size: int = -1) -> bytes:
        payload = self._stream.read(size)
        self._digest.update(payload)
        self.byte_count += len(payload)
        return payload

    def readinto(self, buffer: Any) -> int:
        count = self._stream.readinto(buffer)
        if count:
            self._digest.update(memoryview(buffer)[:count])
            self.byte_count += count
        return count

    @property
    def digest(self) -> str:
        return f"sha256:{self._digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class WireSourceReleaseBundle:
    """The three members of one exchanged release that carry structural shape."""

    root: Mapping[str, Any]
    manifest: Mapping[str, Any]
    items: Sequence[Any]


@dataclass(frozen=True, slots=True)
class _PreparedWireRelease:
    pin: SourceReleasePin
    reference: SourceCatalogRef
    summary: SourceCatalogSummary
    root: Mapping[str, Any]
    manifest: Mapping[str, Any]
    items_path: Path
    items_size: int
    items_digest: str
    items_count: int


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


def _normalized_wire_digest(value: object, label: str) -> str:
    digest = require_text(value, label)
    normalized = digest if digest.startswith("sha256:") else f"sha256:{digest}"
    try:
        return require_sha256(normalized, label)
    except ValueError as error:
        raise IntegrityError(str(error)) from error


def _contained_regular_file(root: Path, relative: object, label: str) -> Path:
    try:
        name = require_relative_path(relative, label)
    except ValueError as error:
        raise IntegrityError(str(error)) from error
    path = root
    for part in name.split("/"):
        path /= part
        if path.is_symlink():
            raise IntegrityError(f"{label} must not traverse a symlink: {name}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise IntegrityError(f"{label} is missing: {name}") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise IntegrityError(f"{label} must be a contained regular file: {name}")
    return resolved


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, f"sha256:{digest.hexdigest()}"


def _verify_member(path: Path, member: Mapping[str, Any], label: str) -> None:
    expected_size = _count(member.get("byteSize"), f"{label} byte size")
    expected_digest = _normalized_wire_digest(member.get("sha256"), f"{label} digest")
    size, digest = _file_digest(path)
    if size != expected_size:
        raise IntegrityError(f"{label} differs in size from its manifest")
    if digest != expected_digest:
        raise IntegrityError(f"{label} differs from its manifest digest")


def _raise_first_violation(conformance: SourceReleaseConformance) -> None:
    if conformance.conforms:
        return
    first = conformance.violations[0]
    raise IntegrityError(
        f"sealed source release violates its published schema, first of {len(conformance.violations)} "
        f"at {first.member}{first.pointer}: {first.message}"
    )


def _wire_state(disposition: str) -> SourceItemState:
    if disposition == "selected":
        return SourceItemState.ACTIVE
    if disposition == "deleted":
        return SourceItemState.DELETED
    if disposition in {"excluded", "unavailable", "failed"}:
        return SourceItemState.EXCLUDED
    raise IntegrityError(f"wire source item has unknown disposition {disposition!r}")


def _source_item(record: Mapping[str, Any], index: int) -> tuple[SourceItem, str]:
    try:
        selection = record["selection"]
        if not isinstance(selection, Mapping):
            raise TypeError("selection is not an object")
        disposition = require_text(selection["disposition"], "wire source item disposition")
        candidates = tuple(
            CandidateFile(
                candidate_id=candidate["renditionId"],
                locator=candidate["locator"],
                media_type=candidate["mediaType"],
                expected_digest=candidate["expectedSha256"],
                expected_size=candidate["expectedByteSize"],
            )
            for candidate in record["candidateRenditions"]
        )
        metadata = {
            "documentId": record["documentId"],
            "normalizedMetadata": record["normalizedMetadata"],
            "selection": dict(selection),
            "sourceNativeMetadata": record["sourceNativeMetadata"],
            "sourceObservations": record["sourceObservations"],
            "sourceObservedTopics": record["sourceObservedTopics"],
        }
        return (
            SourceItem(
                item_id=record["sourceItemId"],
                version=record["sourceIssuedVersion"],
                candidates=candidates,
                state=_wire_state(disposition),
                metadata=metadata,
            ),
            disposition,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError(f"wire source item {index} cannot become a DocSpec SourceItem: {error}") from error


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

        violations = list(self.check_header(root=root, manifest=manifest).violations)
        for index, record in enumerate(items):
            violations.extend(self.check_item(record, index=index).violations)
        return SourceReleaseConformance(tuple(violations))

    def check_header(
        self,
        *,
        root: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> SourceReleaseConformance:
        """Return every release-root and member-manifest violation."""

        return SourceReleaseConformance(
            tuple(
                [
                    *self._violations(WIRE_ROOT_MEMBER, "release-root", root),
                    *self._violations(WIRE_MANIFEST_MEMBER, "member-manifest", manifest),
                ]
            )
        )

    def check_item(self, item: Any, *, index: int) -> SourceReleaseConformance:
        """Return the first structural violation of one source item."""

        return SourceReleaseConformance(
            tuple(self._violations(WIRE_ITEMS_MEMBER, "source-items", item, prefix=f"/{index}")[:1])
        )

    def check_bundle(self, directory: Path, *, max_member_bytes: int = MAX_WIRE_MEMBER_BYTES) -> SourceReleaseConformance:
        """Read one exchanged release bundle from disk and return its structural verdict."""

        bundle = read_wire_release_bundle(directory, max_member_bytes=max_member_bytes)
        return self.check(root=bundle.root, manifest=bundle.manifest, items=bundle.items)


class LocalWireSourceReleaseReader:
    """Admit and stream an exchanged SourceCatalogRelease from one configured root."""

    def __init__(
        self,
        root: Path,
        *,
        wire_gate: SourceReleaseSchemaGate,
        max_root_bytes: int = 8 * 1024**2,
        max_manifest_bytes: int = 64 * 1024**2,
        stream_buffer_bytes: int = 1024 * 1024,
    ) -> None:
        unresolved = Path(root).absolute()
        if unresolved.is_symlink():
            raise IntegrityError(f"wire source release root must not be a symlink: {unresolved}")
        try:
            resolved = unresolved.resolve(strict=True)
        except OSError as error:
            raise IntegrityError(f"wire source release root does not exist: {unresolved}") from error
        if not resolved.is_dir():
            raise IntegrityError(f"wire source release root must be a directory: {resolved}")
        if min(max_root_bytes, max_manifest_bytes, stream_buffer_bytes) <= 0:
            raise ValueError("wire source release byte limits must be positive")
        self.root = resolved
        self._wire_gate = wire_gate
        self._max_root_bytes = max_root_bytes
        self._max_manifest_bytes = max_manifest_bytes
        self._stream_buffer_bytes = stream_buffer_bytes

    def _prepare(self, pin: SourceReleasePin) -> _PreparedWireRelease:
        root_path = _contained_regular_file(self.root, pin.root, "wire source release root")
        if root_path.name != WIRE_ROOT_MEMBER:
            raise IntegrityError(f"wire source release root must be named {WIRE_ROOT_MEMBER}")
        root_payload = _read_bounded(root_path, label="wire source release root", max_bytes=self._max_root_bytes)
        if sha256_digest(root_payload) != pin.digest:
            raise IntegrityError("sealed source release root differs from its pinned digest")
        root_value = thaw_json(parse_closed_json(root_payload, label="wire source release root"))
        if not isinstance(root_value, Mapping):
            raise IntegrityError("wire source release root must be a JSON object")

        bundle_root = root_path.parent
        content = root_value.get("content")
        if not isinstance(content, Mapping):
            raise IntegrityError("wire source release root content must be an object")
        global_manifest = content.get("globalManifest")
        if not isinstance(global_manifest, Mapping):
            raise IntegrityError("wire source release global manifest reference must be an object")
        manifest_path = _contained_regular_file(
            bundle_root,
            global_manifest.get("objectKey"),
            "wire source release global manifest",
        )
        if manifest_path.relative_to(bundle_root).as_posix() != WIRE_MANIFEST_MEMBER:
            raise IntegrityError(f"wire source release global manifest must be {WIRE_MANIFEST_MEMBER}")
        manifest_payload = _read_bounded(
            manifest_path,
            label="wire source release global manifest",
            max_bytes=self._max_manifest_bytes,
        )
        if len(manifest_payload) != _count(global_manifest.get("byteSize"), "global manifest byte size"):
            raise IntegrityError("wire source release global manifest differs in size from its root reference")
        if sha256_digest(manifest_payload) != _normalized_wire_digest(
            global_manifest.get("sha256"), "global manifest digest"
        ):
            raise IntegrityError("wire source release global manifest differs from its root digest")
        manifest_value = thaw_json(parse_closed_json(manifest_payload, label="wire source release global manifest"))
        if not isinstance(manifest_value, Mapping):
            raise IntegrityError("wire source release global manifest must be a JSON object")
        _raise_first_violation(self._wire_gate.check_header(root=root_value, manifest=manifest_value))

        raw_members = manifest_value.get("members")
        if not isinstance(raw_members, list):
            raise IntegrityError("wire source release manifest members must be an array")
        members: dict[str, Mapping[str, Any]] = {}
        paths: dict[str, Path] = {}
        for index, member in enumerate(raw_members):
            if not isinstance(member, Mapping):
                raise IntegrityError(f"wire source release manifest member {index} must be an object")
            try:
                object_key = require_relative_path(member.get("objectKey"), "wire source release member path")
            except ValueError as error:
                raise IntegrityError(str(error)) from error
            if object_key in members:
                raise IntegrityError(f"wire source release manifest repeats member {object_key}")
            members[object_key] = member
            paths[object_key] = _contained_regular_file(bundle_root, object_key, "wire source release member")

        expected_files = {WIRE_ROOT_MEMBER, WIRE_MANIFEST_MEMBER, *members}
        observed_files: set[str] = set()
        for path in bundle_root.rglob("*"):
            if path.is_symlink():
                raise IntegrityError(f"wire source release contains a symlink: {path.relative_to(bundle_root)}")
            if path.is_file():
                observed_files.add(path.relative_to(bundle_root).as_posix())
        if observed_files != expected_files:
            raise IntegrityError("wire source release has missing or extra files")

        items_member = members.get(WIRE_ITEMS_MEMBER)
        if items_member is None or items_member.get("role") != "source-items":
            raise IntegrityError(f"wire source release manifest must declare {WIRE_ITEMS_MEMBER} as source-items")
        for object_key, member in members.items():
            if object_key != WIRE_ITEMS_MEMBER:
                _verify_member(paths[object_key], member, f"wire source release member {object_key}")

        items_size = _count(items_member.get("byteSize"), "source-items member byte size")
        if paths[WIRE_ITEMS_MEMBER].stat().st_size != items_size:
            raise IntegrityError("source-items member differs in size from its manifest")
        items_digest = _normalized_wire_digest(items_member.get("sha256"), "source-items member digest")
        items_count = _count(items_member.get("recordCount"), "source-items member record count")

        counts = content.get("counts")
        coverage = content.get("coverage")
        if not isinstance(counts, Mapping) or not isinstance(coverage, Mapping):
            raise IntegrityError("wire source release counts and coverage must be objects")
        discovered = _count(counts.get("discoveredCount"), "wire discovered count")
        state_counts = {
            SourceItemState.ACTIVE.value: _count(counts.get("selectedCount"), "wire selected count"),
            SourceItemState.DELETED.value: _count(counts.get("deletedCount"), "wire deleted count"),
            SourceItemState.EXCLUDED.value: sum(
                _count(counts.get(name), f"wire {name}")
                for name in ("excludedCount", "unavailableCount", "failedCount")
            ),
        }
        release_id = require_text(root_value.get("releaseId"), "wire source release identity")
        reference = SourceCatalogRef(release_id, pin.root, pin.digest)
        summary = SourceCatalogSummary(release_id, "snapshot", discovered, (), state_counts, coverage)
        return _PreparedWireRelease(
            pin,
            reference,
            summary,
            root_value,
            manifest_value,
            paths[WIRE_ITEMS_MEMBER],
            items_size,
            items_digest,
            items_count,
        )

    def _items(self, prepared: _PreparedWireRelease) -> Iterator[SourceItem]:
        try:
            import ijson  # type: ignore[import-not-found]
        except ImportError as error:
            raise WireSourceReleaseError(
                "install the docspec[wire] extra to read a wire-format source release"
            ) from error

        expected_dispositions = {
            "selected": prepared.summary.state_counts[SourceItemState.ACTIVE.value],
            "deleted": prepared.summary.state_counts[SourceItemState.DELETED.value],
            "excluded": _count(prepared.root["content"]["counts"].get("excludedCount"), "wire excluded count"),
            "unavailable": _count(
                prepared.root["content"]["counts"].get("unavailableCount"), "wire unavailable count"
            ),
            "failed": _count(prepared.root["content"]["counts"].get("failedCount"), "wire failed count"),
        }
        observed_dispositions = dict.fromkeys(expected_dispositions, 0)
        previous_item_id: str | None = None
        count = 0

        with prepared.items_path.open("rb") as stream:
            initial = os.fstat(stream.fileno())
            if stream.read(1) != b"[":
                raise IntegrityError("wire source-items member must be a JSON array")
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"]":
                raise IntegrityError("wire source-items member must end with its JSON array")
            stream.seek(0)
            digesting = _DigestingReader(stream)
            try:
                records = ijson.items(
                    digesting,
                    "item",
                    map_type=_DuplicateSafeDict,
                    buf_size=self._stream_buffer_bytes,
                )
                for index, record in enumerate(records):
                    if not isinstance(record, Mapping):
                        raise IntegrityError(f"wire source item {index} must be an object")
                    _raise_first_violation(self._wire_gate.check_item(record, index=index))
                    item, disposition = _source_item(record, index)
                    if previous_item_id is not None and item.item_id <= previous_item_id:
                        raise IntegrityError("wire source items are not strictly ordered by sourceItemId")
                    previous_item_id = item.item_id
                    observed_dispositions[disposition] += 1
                    count += 1
                    yield item
            except IntegrityError:
                raise
            except Exception as error:  # noqa: BLE001 - ijson backends expose backend-specific parse errors
                raise IntegrityError(f"wire source-items member is not valid duplicate-safe JSON: {error}") from error
            final = os.fstat(stream.fileno())

        if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        ):
            raise IntegrityError("wire source-items member changed while it was read")
        if digesting.byte_count != prepared.items_size:
            raise IntegrityError("wire source-items member differs in size from its manifest")
        if digesting.digest != prepared.items_digest:
            raise IntegrityError("wire source-items member differs from its manifest digest")
        if count != prepared.items_count:
            raise IntegrityError("wire source-items record count differs from its manifest")
        if count != prepared.summary.item_count:
            raise IntegrityError("wire source-items record count differs from the release")
        if observed_dispositions != expected_dispositions:
            raise IntegrityError("wire source-item dispositions differ from the release counts")

    @staticmethod
    def _admission(prepared: _PreparedWireRelease) -> SourceReleaseAdmission:
        return SourceReleaseAdmission(prepared.pin, prepared.reference, prepared.summary)

    def admit(self, pin: SourceReleasePin) -> SourceReleaseAdmission:
        prepared = self._prepare(pin)
        for _ in self._items(prepared):
            pass
        return self._admission(prepared)

    def open(self, pin: SourceReleasePin) -> SourceReleaseRead:
        prepared = self._prepare(pin)
        return SourceReleaseRead(self._admission(prepared), self._items(prepared))


__all__ = [
    "JsonSchemaWireSourceReleaseGate",
    "LocalWireSourceReleaseReader",
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
