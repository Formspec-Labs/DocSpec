"""Strict local source-catalog distributions, sealed-release admission, and contained file acquisition."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docspec.adapters.storage import (
    _contained,
    _iter_canonical_json_lines,
    _publish_directory_once,
    _read_exact,
    _storage_root,
    _verified_member_path,
    _write_once,
)
from docspec.adapters.wire_source_release import read_wire_release_bundle
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    identity_digest,
    parse_canonical_json,
    parse_closed_json,
    require_relative_path,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream
from docspec.ports.source_catalog import SourceCatalogRead, SourceCatalogSummary
from docspec.ports.source_release import (
    SourceReleaseAdmission,
    SourceReleasePin,
    SourceReleaseRead,
    SourceReleaseSchemaGate,
)

LOCAL_CATALOG_FORMAT = "docspec-source-catalog"


class LocalJsonlSourceCatalog:
    """Publish and read complete canonical JSON/JSONL catalog distributions."""

    def __init__(
        self,
        root: Path,
        *,
        max_item_bytes: int = 8 * 1024**2,
        max_root_bytes: int = 8 * 1024**2,
    ) -> None:
        if min(max_item_bytes, max_root_bytes) <= 0:
            raise ValueError("source catalog byte limits must be positive")
        self.root = _storage_root(root)
        self.max_item_bytes = max_item_bytes
        self.max_root_bytes = max_root_bytes
        self._staging = _contained(self.root, ".staging/catalogs", create_parents=True).parent
        self._staging.mkdir(exist_ok=True)

    @staticmethod
    def _distribution_key(catalog_id: str) -> str:
        return hashlib.sha256(catalog_id.encode("utf-8")).hexdigest()

    def write(
        self,
        items: Iterable[SourceItem],
        *,
        kind: str = "snapshot",
        base_catalog: SourceCatalogRef | None = None,
        partitions: tuple[str, ...] = (),
        coverage: Mapping[str, Any] | None = None,
    ) -> SourceCatalogRef:
        if kind not in {"snapshot", "change-set"}:
            raise ValueError("source catalog kind must be snapshot or change-set")
        if kind == "change-set" and base_catalog is None:
            raise ValueError("a source catalog change set must identify its base")
        if tuple(sorted(set(partitions))) != partitions:
            raise ValueError("source catalog partitions must be sorted and distinct")
        work = Path(tempfile.mkdtemp(prefix="catalog-", dir=self._staging))
        member_path = work / "items.jsonl"
        item_count = 0
        state_counts = {state.value: 0 for state in SourceItemState}
        previous: tuple[str, str] | None = None
        try:
            digest = hashlib.sha256()
            byte_size = 0
            with member_path.open("xb") as handle:
                for item in items:
                    key = (item.item_id, item.version)
                    if previous is not None and key <= previous:
                        raise IntegrityError("source catalog items must be strictly ordered by item identity and version")
                    if previous is not None and item.item_id == previous[0]:
                        raise IntegrityError("source catalog contains more than one current record for an item identity")
                    previous = key
                    line = canonical_json_bytes(item.to_dict()) + b"\n"
                    if len(line) > self.max_item_bytes:
                        raise LimitExceededError(f"source catalog item exceeds the {self.max_item_bytes}-byte limit")
                    handle.write(line)
                    digest.update(line)
                    byte_size += len(line)
                    item_count += 1
                    state_counts[item.state.value] += 1
                handle.flush()
                os.fsync(handle.fileno())
            member = {
                "path": "items.jsonl",
                "mediaType": "application/x-ndjson",
                "byteSize": byte_size,
                "digest": f"sha256:{digest.hexdigest()}",
                "recordCount": item_count,
            }
            content = {
                "kind": kind,
                "baseCatalog": None if base_catalog is None else base_catalog.to_dict(),
                "itemsMember": member,
                "counts": {"items": item_count, "states": state_counts},
                "partitions": list(partitions),
                "coverage": {} if coverage is None else dict(coverage),
            }
            catalog_id = stable_urn("source-catalog", content)
            root = {
                "format": LOCAL_CATALOG_FORMAT,
                "formatVersion": "1.0",
                "catalogId": catalog_id,
                **content,
            }
            root_payload = canonical_json_file_bytes(root)
            if len(root_payload) > self.max_root_bytes:
                raise LimitExceededError(f"source catalog root exceeds the {self.max_root_bytes}-byte limit")
            _write_once(work.resolve(strict=True), "catalog.json", root_payload)
            key = self._distribution_key(catalog_id)
            distribution_locator = f"source-catalogs/{key[:2]}/{key}"
            _publish_directory_once(self.root, work, distribution_locator)
            reference = SourceCatalogRef(
                catalog_id,
                f"{distribution_locator}/catalog.json",
                sha256_digest(root_payload),
            )
            self.verify(reference)
            return reference
        finally:
            if work.exists():
                shutil.rmtree(work)

    def _open_root(self, reference: SourceCatalogRef) -> tuple[dict[str, Any], Path]:
        path = _contained(self.root, reference.locator)
        if path.is_file() and path.stat().st_size > self.max_root_bytes:
            raise LimitExceededError(f"source catalog root exceeds the {self.max_root_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("source catalog root differs from its reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.catalog_id))
        if not isinstance(value, dict):
            raise IntegrityError("source catalog root must be a JSON object")
        expected = {
            "format",
            "formatVersion",
            "catalogId",
            "kind",
            "baseCatalog",
            "itemsMember",
            "counts",
            "partitions",
            "coverage",
        }
        if set(value) != expected or value["format"] != LOCAL_CATALOG_FORMAT or value["formatVersion"] != "1.0":
            raise IntegrityError("source catalog root has an unknown format or invalid closed shape")
        content = {name: value[name] for name in expected - {"format", "formatVersion", "catalogId"}}
        if value["catalogId"] != stable_urn("source-catalog", content) or value["catalogId"] != reference.catalog_id:
            raise IntegrityError("source catalog identity differs from its canonical content")
        key = self._distribution_key(reference.catalog_id)
        expected_locator = f"source-catalogs/{key[:2]}/{key}/catalog.json"
        if reference.locator != expected_locator:
            raise IntegrityError("source catalog locator differs from its identity")
        distribution = _contained(self.root, reference.locator).parent
        if distribution.is_symlink() or not distribution.is_dir():
            raise IntegrityError("source catalog distribution is not a regular directory")
        members = {path.name for path in distribution.iterdir()}
        if members != {"catalog.json", "items.jsonl"} or any(path.is_symlink() for path in distribution.iterdir()):
            raise IntegrityError("source catalog distribution has missing, extra, or symlinked members")
        return value, distribution

    def _validated_items(self, reference: SourceCatalogRef) -> tuple[dict[str, Any], Iterator[SourceItem]]:
        root, distribution = self._open_root(reference)
        member = root["itemsMember"]
        try:
            path = _verified_member_path(distribution, member, media_type="application/x-ndjson")
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"source catalog member description is invalid: {error}") from error

        def generate() -> Iterator[SourceItem]:
            previous: tuple[str, str] | None = None
            count = 0
            state_counts = {state.value: 0 for state in SourceItemState}
            for value in _iter_canonical_json_lines(
                path,
                label="source catalog items",
                max_line_bytes=self.max_item_bytes,
            ):
                try:
                    item = SourceItem.from_dict(value)
                except (TypeError, ValueError) as error:
                    raise IntegrityError(f"source catalog item is invalid: {error}") from error
                key = (item.item_id, item.version)
                if previous is not None and key <= previous:
                    raise IntegrityError("source catalog items are not strictly ordered")
                if previous is not None and item.item_id == previous[0]:
                    raise IntegrityError("source catalog repeats one source-item identity")
                previous = key
                count += 1
                state_counts[item.state.value] += 1
                yield item
            if count != member["recordCount"] or root["counts"] != {"items": count, "states": state_counts}:
                raise IntegrityError("source catalog counts differ from its member records")

        return root, generate()

    @staticmethod
    def _summary(reference: SourceCatalogRef, root: Mapping[str, Any]) -> SourceCatalogSummary:
        if root["kind"] not in {"snapshot", "change-set"}:
            raise IntegrityError("source catalog kind is unknown")
        base_value = root["baseCatalog"]
        if root["kind"] == "change-set" and base_value is None:
            raise IntegrityError("source catalog change set does not identify its base")
        try:
            base = None if base_value is None else SourceCatalogRef.from_dict(base_value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"source catalog base reference is invalid: {error}") from error
        partitions = root["partitions"]
        if not isinstance(partitions, list) or any(not isinstance(item, str) or not item for item in partitions):
            raise IntegrityError("source catalog partitions must be non-empty strings")
        if partitions != sorted(set(partitions)):
            raise IntegrityError("source catalog partitions must be sorted and distinct")
        counts = root["counts"]
        if not isinstance(counts, dict) or set(counts) != {"items", "states"} or not isinstance(counts["states"], dict):
            raise IntegrityError("source catalog counts have an invalid closed shape")
        return SourceCatalogSummary(
            reference.catalog_id,
            root["kind"],
            counts["items"],
            tuple(partitions),
            counts["states"],
            root["coverage"],
            base,
        )

    def describe(self, reference: SourceCatalogRef) -> SourceCatalogSummary:
        return self.verify(reference)

    def open(self, reference: SourceCatalogRef) -> SourceCatalogRead:
        """Verify membership once and return the one validating record stream."""

        root, items = self._validated_items(reference)
        return SourceCatalogRead(self._summary(reference, root), items)

    def verify(self, reference: SourceCatalogRef) -> SourceCatalogSummary:
        root, items = self._validated_items(reference)
        summary = self._summary(reference, root)
        for _ in items:
            pass
        return summary

    def stream(self, reference: SourceCatalogRef) -> Iterator[SourceItem]:
        yield from self.open(reference).items


class LocalSourceReleaseReader:
    """Admit sealed local source releases by digest through one injected catalog.

    An optional `wire_gate` screens any pinned root that is not this reader's
    own `docspec-source-catalog` distribution. That format is not the exchanged
    wire format, so the gate never runs for a local release; it runs when a
    release published in the exchanged format arrives at this pin, and turns a
    confusing lower-layer refusal into a located structural one.
    """

    def __init__(self, catalog: LocalJsonlSourceCatalog, *, wire_gate: SourceReleaseSchemaGate | None = None) -> None:
        self._catalog = catalog
        self._wire_gate = wire_gate

    def _screen(self, pin: SourceReleasePin, payload: bytes) -> None:
        """Refuse a digest-verified root that this reader's own distribution does not describe."""

        value = parse_closed_json(payload, label="sealed source release root")
        if isinstance(value, Mapping) and value.get("format") == LOCAL_CATALOG_FORMAT:
            return
        bundle = read_wire_release_bundle(_contained(self._catalog.root, pin.root).parent)
        if bundle.root != thaw_json(value):
            raise IntegrityError("sealed source release root changed while its bundle was read")
        conformance = self._wire_gate.check(root=bundle.root, manifest=bundle.manifest, items=bundle.items)
        if conformance.conforms:
            raise IntegrityError("sealed source release is published in a format this reader does not admit")
        first = conformance.violations[0]
        raise IntegrityError(
            f"sealed source release violates its published schema, first of {len(conformance.violations)} "
            f"at {first.member}{first.pointer}: {first.message}"
        )

    def _reference(self, pin: SourceReleasePin) -> SourceCatalogRef:
        """Recompute the pinned root digest and read the identity those bytes carry."""

        limit = self._catalog.max_root_bytes
        path = _contained(self._catalog.root, pin.root)
        if path.is_file() and path.stat().st_size > limit:
            raise LimitExceededError(f"sealed source release root exceeds the {limit}-byte limit")
        payload = _read_exact(self._catalog.root, pin.root)
        if sha256_digest(payload) != pin.digest:
            raise IntegrityError("sealed source release root differs from its pinned digest")
        if self._wire_gate is not None:
            self._screen(pin, payload)
        value = thaw_json(parse_canonical_json(payload, label="sealed source release root"))
        identity = value.get("catalogId") if isinstance(value, dict) else None
        if not isinstance(identity, str) or not identity:
            raise IntegrityError("sealed source release root does not name one release identity")
        return SourceCatalogRef(identity, pin.root, pin.digest)

    def admit(self, pin: SourceReleasePin) -> SourceReleaseAdmission:
        reference = self._reference(pin)
        return SourceReleaseAdmission(pin, reference, self._catalog.verify(reference))

    def open(self, pin: SourceReleasePin) -> SourceReleaseRead:
        reference = self._reference(pin)
        read = self._catalog.open(reference)
        return SourceReleaseRead(SourceReleaseAdmission(pin, reference, read.summary), read.items)


class LocalFileContentFetcher:
    """Stream only regular files contained below one configured local root."""

    downloader_id = "docspec.content-fetcher.local-file.v1"

    def __init__(self, root: Path, *, chunk_size: int = 1024 * 1024) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.root = _storage_root(root)
        self.chunk_size = chunk_size
        self.configuration_digest = identity_digest(
            {"implementationId": self.downloader_id, "root": self.root.as_posix(), "chunkSize": chunk_size}
        )

    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> FetchStream:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        require_text(task_id, "task_id")
        require_text(attempt_id, "attempt_id")
        locator = require_relative_path(candidate.locator, "candidate locator")
        path = _contained(self.root, locator)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        initial = os.fstat(descriptor)
        os.close(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise IntegrityError("local acquisition candidate is not a regular file")
        if initial.st_size > max_bytes:
            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
        if candidate.expected_size is not None and candidate.expected_size != initial.st_size:
            raise IntegrityError("local acquisition candidate differs from its expected size")
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        version = candidate.transport_version or f"local-stat:{initial.st_dev}:{initial.st_ino}:{initial.st_size}:{initial.st_mtime_ns}"

        def chunks() -> Iterator[bytes]:
            seen = 0
            opened: int | None = os.open(path, flags)
            try:
                current = os.fstat(opened)
                if (
                    current.st_dev != initial.st_dev
                    or current.st_ino != initial.st_ino
                    or current.st_size != initial.st_size
                    or current.st_mtime_ns != initial.st_mtime_ns
                ):
                    raise IntegrityError("local acquisition candidate changed before it was read")
                handle = os.fdopen(opened, "rb")
                opened = None  # ownership transferred to the file object
                with handle:
                    for chunk in iter(lambda: handle.read(self.chunk_size), b""):
                        seen += len(chunk)
                        if seen > max_bytes:
                            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
                        yield chunk
                    final = os.fstat(handle.fileno())
                if (
                    seen != initial.st_size
                    or final.st_dev != initial.st_dev
                    or final.st_ino != initial.st_ino
                    or final.st_mtime_ns != initial.st_mtime_ns
                ):
                    raise IntegrityError("local acquisition candidate changed while it was read")
            finally:
                if opened is not None:
                    try:
                        os.close(opened)
                    except OSError:
                        pass

        return FetchStream(
            FetchMetadata(
                self.downloader_id,
                self.configuration_digest,
                version,
                started_at,
                task_id,
                attempt_id,
            ),
            chunks(),
        )


__all__ = ["LocalFileContentFetcher", "LocalJsonlSourceCatalog", "LocalSourceReleaseReader"]
