"""Load and materialize sealed, byte-exact conformance fixture distributions."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from docspec.domain.identity import (
    canonical_json_file_bytes,
    freeze_json,
    parse_canonical_json,
    require_relative_path,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.errors import IntegrityError

_MANIFEST_NAME = "fixture-set.json"
_MAX_MANIFEST_BYTES = 8 * 1024**2
_MAX_MEMBER_BYTES = 64 * 1024**2
_MANIFEST_KEYS = {
    "format",
    "formatVersion",
    "fixtureSetId",
    "suite",
    "cases",
    "members",
}
_CASE_KEYS = {
    "caseId",
    "contract",
    "expectedOutcome",
    "expectedValue",
    "inputReference",
    "layout",
}
_OUTCOME_KEYS = {"verdict", "failureCode", "messagePattern"}
_LAYOUT_KEYS = {"payload", "target"}
_MEMBER_KEYS = {"path", "mediaType", "byteSize", "digest"}
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_CHUNK_BYTES = 1024 * 1024


class FixtureCaseDiagnostic(StrEnum):
    """Stable diagnostic codes emitted while checking sealed negative cases."""

    SOURCE_CATALOG_BROKEN_ANCESTRY = "DOCSPEC-SOURCE-CATALOG-BROKEN-ANCESTRY"
    SOURCE_CATALOG_DUPLICATE_IDENTITY = "DOCSPEC-SOURCE-CATALOG-DUPLICATE-IDENTITY"
    SOURCE_CATALOG_EXTRA_MEMBER = "DOCSPEC-SOURCE-CATALOG-EXTRA-MEMBER"
    SOURCE_CATALOG_IDENTITY_DRIFT = "DOCSPEC-SOURCE-CATALOG-IDENTITY-DRIFT"
    SOURCE_CATALOG_MEMBER_DIGEST_MISMATCH = "DOCSPEC-SOURCE-CATALOG-MEMBER-DIGEST-MISMATCH"
    SOURCE_CATALOG_MISSING_MEMBER = "DOCSPEC-SOURCE-CATALOG-MISSING-MEMBER"
    SOURCE_CATALOG_PATH_ESCAPE = "DOCSPEC-SOURCE-CATALOG-PATH-ESCAPE"
    SOURCE_CATALOG_ROOT_DIGEST_MISMATCH = "DOCSPEC-SOURCE-CATALOG-ROOT-DIGEST-MISMATCH"
    SOURCE_CATALOG_UNKNOWN_FORMAT = "DOCSPEC-SOURCE-CATALOG-UNKNOWN-FORMAT"


_CASE_DIAGNOSTICS: dict[str, tuple[str, FixtureCaseDiagnostic, re.Pattern[str]]] = {
    "source-catalog-broken-change-ancestry": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_BROKEN_ANCESTRY,
        re.compile(r"does not identify its base"),
    ),
    "source-catalog-digest-mismatch": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_ROOT_DIGEST_MISMATCH,
        re.compile(r"root differs from its reference"),
    ),
    "source-catalog-duplicate-logical-identity": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_DUPLICATE_IDENTITY,
        re.compile(r"repeats one source-item identity"),
    ),
    "source-catalog-extra-member": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_EXTRA_MEMBER,
        re.compile(r"missing, extra, or symlinked members"),
    ),
    "source-catalog-identity-drift": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_IDENTITY_DRIFT,
        re.compile(r"identity differs"),
    ),
    "source-catalog-member-digest-mismatch": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_MEMBER_DIGEST_MISMATCH,
        re.compile(r"member bytes differ"),
    ),
    "source-catalog-missing-member": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_MISSING_MEMBER,
        re.compile(r"missing, extra, or symlinked members"),
    ),
    "source-catalog-path-escape": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_PATH_ESCAPE,
        re.compile(r"contained relative path"),
    ),
    "source-catalog-unknown-format": (
        "SOURCE-CATALOG-CONTRACT",
        FixtureCaseDiagnostic.SOURCE_CATALOG_UNKNOWN_FORMAT,
        re.compile(r"unknown format"),
    ),
}


def _require_closed(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise IntegrityError(f"{label} has an invalid closed shape; missing={missing}, extra={extra}")


def diagnostic_code_for_rejection(case: FixtureCase, error: BaseException) -> FixtureCaseDiagnostic:
    """Classify a contract rejection through the registered stable diagnostic set."""

    diagnostic = _CASE_DIAGNOSTICS.get(case.case_id)
    if diagnostic is None or diagnostic[0] != case.contract:
        raise IntegrityError(f"fixture case has no rejection diagnostic registry: {case.case_id}")
    _, code, pattern = diagnostic
    if pattern.search(str(error)) is None:
        raise IntegrityError(
            f"fixture rejection does not match its stable diagnostic; case={case.case_id}, code={code.value}"
        ) from error
    return code


def _same_node(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_safe_filesystem_primitives() -> None:
    dir_fd_functions = (os.mkdir, os.open, os.stat)
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or any(function not in os.supports_dir_fd for function in dir_fd_functions)
        or os.scandir not in os.supports_fd
    ):
        raise IntegrityError("fixture verification requires descriptor-relative no-follow filesystem operations")


def _open_child_directory(parent_descriptor: int, name: str, *, create: bool = False) -> int:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise IntegrityError(f"fixture directory component is unsafe: {name!r}")
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileExistsError:
            pass
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise IntegrityError(f"fixture path component is not a regular directory: {name}")
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        raise IntegrityError(f"fixture directory cannot be opened without following links: {name}") from error
    after = os.fstat(descriptor)
    if not stat.S_ISDIR(after.st_mode) or not _same_node(before, after):
        os.close(descriptor)
        raise IntegrityError(f"fixture directory changed while it was opened: {name}")
    return descriptor


@contextmanager
def _open_directory_path(path: Path, *, create: bool = False) -> Iterator[tuple[Path, int]]:
    _require_safe_filesystem_primitives()
    absolute = _absolute(Path(path))
    anchor = absolute.anchor
    if not anchor:
        raise IntegrityError(f"fixture directory path has no anchor: {path}")
    try:
        descriptor = os.open(anchor, _DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise IntegrityError(f"fixture directory anchor cannot be opened: {anchor}") from error
    try:
        for part in absolute.parts[1:]:
            child = _open_child_directory(descriptor, part, create=create)
            os.close(descriptor)
            descriptor = child
        yield absolute, descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_parent_at(root_descriptor: int, relative: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    parts = PurePosixPath(require_relative_path(relative, "fixture relative path")).parts
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            child = _open_child_directory(descriptor, part, create=create)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _read_regular_at(root_descriptor: int, relative: str, *, max_bytes: int, label: str) -> bytes:
    with _open_parent_at(root_descriptor, relative) as (parent_descriptor, name):
        try:
            before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as error:
            raise IntegrityError(f"{label} is missing or cannot be inspected safely: {relative}") from error
        if not stat.S_ISREG(before.st_mode):
            raise IntegrityError(f"{label} must be a regular, non-symlink file: {relative}")
        if before.st_size > max_bytes:
            raise IntegrityError(f"{label} exceeds its {max_bytes}-byte limit: {relative}")
        try:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_descriptor)
        except OSError as error:
            raise IntegrityError(f"{label} cannot be opened without following links: {relative}") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not _same_node(before, opened):
                raise IntegrityError(f"{label} changed while it was opened: {relative}")
            chunks: list[bytes] = []
            byte_size = 0
            while chunk := os.read(descriptor, min(_READ_CHUNK_BYTES, max_bytes + 1 - byte_size)):
                byte_size += len(chunk)
                if byte_size > max_bytes:
                    raise IntegrityError(f"{label} exceeds its {max_bytes}-byte limit: {relative}")
                chunks.append(chunk)
            finished = os.fstat(descriptor)
            if (
                not _same_node(opened, finished)
                or opened.st_size != finished.st_size
                or opened.st_mtime_ns != finished.st_mtime_ns
                or byte_size != finished.st_size
            ):
                raise IntegrityError(f"{label} changed while it was read: {relative}")
            return b"".join(chunks)
        finally:
            os.close(descriptor)


def _write_regular_at(root_descriptor: int, relative: str, payload: bytes) -> None:
    with _open_parent_at(root_descriptor, relative, create=True) as (parent_descriptor, name):
        try:
            descriptor = os.open(name, _FILE_WRITE_FLAGS, 0o600, dir_fd=parent_descriptor)
        except OSError as error:
            raise IntegrityError(f"fixture target cannot be created immutably: {relative}") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise IntegrityError(f"fixture target is not a regular file: {relative}")
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise IntegrityError(f"fixture target write made no progress: {relative}")
                remaining = remaining[written:]
            os.fsync(descriptor)
            finished = os.fstat(descriptor)
            if not _same_node(opened, finished) or finished.st_size != len(payload):
                raise IntegrityError(f"fixture target changed while it was written: {relative}")
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)


def _inventory_directory(descriptor: int, *, prefix: PurePosixPath = PurePosixPath()) -> set[str]:
    files: set[str] = set()
    try:
        entries = sorted(os.scandir(descriptor), key=lambda entry: entry.name)
    except OSError as error:
        raise IntegrityError(f"fixture directory cannot be enumerated safely: {prefix.as_posix()}") from error
    if prefix.parts and not entries:
        raise IntegrityError(f"fixture distribution contains an empty directory: {prefix.as_posix()}")
    for entry in entries:
        relative = prefix / entry.name
        try:
            observed = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise IntegrityError(f"fixture node cannot be inspected safely: {relative.as_posix()}") from error
        if stat.S_ISREG(observed.st_mode):
            files.add(relative.as_posix())
            continue
        if stat.S_ISDIR(observed.st_mode):
            child = _open_child_directory(descriptor, entry.name)
            try:
                files.update(_inventory_directory(child, prefix=relative))
            finally:
                os.close(child)
            continue
        raise IntegrityError(f"fixture distribution contains an undeclared special node: {relative.as_posix()}")
    return files


@dataclass(frozen=True, slots=True)
class FixtureMember:
    path: str
    media_type: str
    byte_size: int
    digest: str
    _root: Path

    def read_bytes(self) -> bytes:
        """Read the declared exact bytes and verify them again at point of use."""

        with _open_directory_path(self._root) as (_, root_descriptor):
            payload = _read_regular_at(
                root_descriptor,
                self.path,
                max_bytes=_MAX_MEMBER_BYTES,
                label="fixture member",
            )
        if len(payload) != self.byte_size or sha256_digest(payload) != self.digest:
            raise IntegrityError(f"fixture member changed after verification: {self.path}")
        return payload


@dataclass(frozen=True, slots=True)
class FixtureLayoutMember:
    payload: str
    target: str


@dataclass(frozen=True, slots=True)
class FixtureExpectedOutcome:
    verdict: str
    failure_code: FixtureCaseDiagnostic | None
    message_pattern: str | None


@dataclass(frozen=True, slots=True)
class FixtureCase:
    case_id: str
    contract: str
    expected_outcome: FixtureExpectedOutcome
    expected_value: Mapping[str, Any] | None
    input_reference: Mapping[str, Any]
    layout: tuple[FixtureLayoutMember, ...]


@dataclass(frozen=True, slots=True)
class FixtureDistribution:
    fixture_set_id: str
    suite: str
    cases: tuple[FixtureCase, ...]
    members: tuple[FixtureMember, ...]

    def case(self, case_id: str) -> FixtureCase:
        matches = [case for case in self.cases if case.case_id == case_id]
        if len(matches) != 1:
            raise KeyError(f"fixture case is not present exactly once: {case_id}")
        return matches[0]

    def cases_for(self, contract: str) -> tuple[FixtureCase, ...]:
        return tuple(case for case in self.cases if case.contract == contract)

    def materialize(self, case: FixtureCase | str, destination: Path) -> Path:
        """Copy one case's sealed bytes through no-follow descriptors into a fresh directory."""

        selected = self.case(case) if isinstance(case, str) else case
        if selected not in self.cases:
            raise ValueError("fixture case does not belong to this distribution")
        destination = _absolute(Path(destination))
        if not destination.name or destination == Path(destination.anchor):
            raise IntegrityError(f"fixture destination must name a new child directory: {destination}")
        members = {member.path: member for member in self.members}
        with _open_directory_path(destination.parent, create=True) as (_, parent_descriptor):
            try:
                os.mkdir(destination.name, mode=0o700, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except FileExistsError as error:
                raise IntegrityError(f"fixture destination must not already exist: {destination}") from error
            except OSError as error:
                raise IntegrityError(f"fixture destination cannot be created safely: {destination}") from error
            root_descriptor = _open_child_directory(parent_descriptor, destination.name)
        try:
            for item in selected.layout:
                _write_regular_at(root_descriptor, item.target, members[item.payload].read_bytes())
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
        return destination


def load_fixture_distribution(root: Path) -> FixtureDistribution:
    """Independently verify one closed fixture root and return its byte references."""

    root = _absolute(Path(root))
    with _open_directory_path(root) as (_, root_descriptor):
        raw_manifest = _read_regular_at(
            root_descriptor,
            _MANIFEST_NAME,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="fixture manifest",
        )
    value = thaw_json(parse_canonical_json(raw_manifest, label="conformance fixture manifest"))
    if not isinstance(value, dict):
        raise IntegrityError("fixture manifest must be a JSON object")
    _require_closed(value, _MANIFEST_KEYS, label="fixture manifest")
    if value["format"] != "docspec-conformance-fixture-set" or value["formatVersion"] != "1.0":
        raise IntegrityError("fixture manifest has an unknown format")
    if raw_manifest != canonical_json_file_bytes(value):
        raise IntegrityError("fixture manifest is not canonical JSON")
    suite = require_text(value["suite"], "fixture suite")

    raw_members = value["members"]
    if not isinstance(raw_members, list) or not raw_members:
        raise IntegrityError("fixture manifest members must be a non-empty list")
    members: list[FixtureMember] = []
    for index, raw_member in enumerate(raw_members):
        if not isinstance(raw_member, dict):
            raise IntegrityError(f"fixture member {index} must be an object")
        _require_closed(raw_member, _MEMBER_KEYS, label=f"fixture member {index}")
        path = require_relative_path(raw_member["path"], f"fixture member {index} path")
        media_type = require_text(raw_member["mediaType"], f"fixture member {index} media type")
        byte_size = raw_member["byteSize"]
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or not 0 <= byte_size <= _MAX_MEMBER_BYTES:
            raise IntegrityError(f"fixture member {path} has an invalid byte size")
        digest = require_sha256(raw_member["digest"], f"fixture member {path} digest")
        member = FixtureMember(path, media_type, byte_size, digest, root)
        member.read_bytes()
        members.append(member)
    member_paths = [member.path for member in members]
    if member_paths != sorted(set(member_paths)):
        raise IntegrityError("fixture members must be sorted and distinct")
    with _open_directory_path(root) as (_, root_descriptor):
        actual_paths = _inventory_directory(root_descriptor)
    actual_paths.discard(_MANIFEST_NAME)
    if actual_paths != set(member_paths):
        missing = sorted(set(member_paths) - actual_paths)
        extra = sorted(actual_paths - set(member_paths))
        raise IntegrityError(f"fixture distribution membership differs; missing={missing}, extra={extra}")

    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise IntegrityError("fixture manifest cases must be a non-empty list")
    member_path_set = set(member_paths)
    referenced_members: set[str] = set()
    cases: list[FixtureCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise IntegrityError(f"fixture case {index} must be an object")
        _require_closed(raw_case, _CASE_KEYS, label=f"fixture case {index}")
        case_id = require_text(raw_case["caseId"], f"fixture case {index} identity")
        contract = require_text(raw_case["contract"], f"fixture case {case_id} contract")
        raw_outcome = raw_case["expectedOutcome"]
        if not isinstance(raw_outcome, dict):
            raise IntegrityError(f"fixture case {case_id} expected outcome must be an object")
        _require_closed(raw_outcome, _OUTCOME_KEYS, label=f"fixture case {case_id} expected outcome")
        verdict = raw_outcome["verdict"]
        raw_failure_code = raw_outcome["failureCode"]
        message_pattern = raw_outcome["messagePattern"]
        if verdict not in {"accept", "reject"}:
            raise IntegrityError(f"fixture case {case_id} has an unknown expected verdict")
        if verdict == "accept":
            if raw_failure_code is not None or message_pattern is not None:
                raise IntegrityError(f"accepted fixture case {case_id} must not declare failure evidence")
            failure_code = None
        else:
            if (
                not isinstance(raw_failure_code, str)
                or not raw_failure_code
                or not isinstance(message_pattern, str)
                or not message_pattern
            ):
                raise IntegrityError(f"rejected fixture case {case_id} must declare failure evidence")
            try:
                failure_code = FixtureCaseDiagnostic(raw_failure_code)
            except ValueError as error:
                raise IntegrityError(f"fixture case {case_id} declares an unknown failure code") from error
            registered = _CASE_DIAGNOSTICS.get(case_id)
            if (
                registered is None
                or registered[0] != contract
                or registered[1] != failure_code
                or registered[2].pattern != message_pattern
            ):
                raise IntegrityError(f"fixture case {case_id} failure evidence differs from its diagnostic registry")
        raw_reference = raw_case["inputReference"]
        if not isinstance(raw_reference, dict):
            raise IntegrityError(f"fixture case {case_id} input reference must be an object")
        reference = freeze_json(raw_reference, label=f"fixture case {case_id} input reference")
        raw_expected = raw_case["expectedValue"]
        if raw_expected is not None and not isinstance(raw_expected, dict):
            raise IntegrityError(f"fixture case {case_id} expected value must be an object or null")
        expected = None if raw_expected is None else freeze_json(raw_expected, label=f"fixture case {case_id} expected value")
        raw_layout = raw_case["layout"]
        if not isinstance(raw_layout, list) or not raw_layout:
            raise IntegrityError(f"fixture case {case_id} layout must be a non-empty list")
        layout: list[FixtureLayoutMember] = []
        targets: list[str] = []
        for layout_index, raw_layout_member in enumerate(raw_layout):
            if not isinstance(raw_layout_member, dict):
                raise IntegrityError(f"fixture case {case_id} layout member {layout_index} must be an object")
            _require_closed(
                raw_layout_member,
                _LAYOUT_KEYS,
                label=f"fixture case {case_id} layout member {layout_index}",
            )
            payload = require_relative_path(raw_layout_member["payload"], "fixture payload path")
            target = require_relative_path(raw_layout_member["target"], "fixture target path")
            if payload not in member_path_set:
                raise IntegrityError(f"fixture case {case_id} names undeclared payload {payload}")
            referenced_members.add(payload)
            targets.append(target)
            layout.append(FixtureLayoutMember(payload, target))
        if targets != sorted(set(targets)):
            raise IntegrityError(f"fixture case {case_id} target paths must be sorted and distinct")
        cases.append(
            FixtureCase(
                case_id,
                contract,
                FixtureExpectedOutcome(verdict, failure_code, message_pattern),
                expected,
                reference,
                tuple(layout),
            )
        )
    case_ids = [case.case_id for case in cases]
    if case_ids != sorted(set(case_ids)):
        raise IntegrityError("fixture cases must be sorted and distinct")
    if referenced_members != member_path_set:
        raise IntegrityError(f"fixture members are not all used by a case: {sorted(member_path_set - referenced_members)}")

    identity_content = {
        "suite": suite,
        "cases": value["cases"],
        "members": value["members"],
    }
    fixture_set_id = require_text(value["fixtureSetId"], "fixture set identity")
    if fixture_set_id != stable_urn("conformance-fixture-set", identity_content):
        raise IntegrityError("fixture set identity differs from its canonical content")
    return FixtureDistribution(fixture_set_id, suite, tuple(cases), tuple(members))


__all__ = [
    "FixtureCase",
    "FixtureCaseDiagnostic",
    "FixtureDistribution",
    "FixtureExpectedOutcome",
    "FixtureLayoutMember",
    "FixtureMember",
    "diagnostic_code_for_rejection",
    "load_fixture_distribution",
]
