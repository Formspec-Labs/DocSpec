"""Local stores: document job revisions and planned-task ledgers."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from docspec.adapters.storage.files import (
    _contained,
    _iter_canonical_json_lines,
    _read_exact,
    _storage_root,
    _verified_member_path,
    _write_once,
)
from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    ordered_json_sequence_digest,
    parse_canonical_json,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.jobs import DocumentEntry, DocumentStore, StoreState
from docspec.domain.references import LayerRef, StoreRef
from docspec.errors import IntegrityError, LimitExceededError, StateTransitionError

_DOCUMENT_STORE_PROFILE_ID = "urn:docspec:profile:document-store-persistence:local-json:1"


_PLANNED_STORE_LAYER_KIND = "planned-document-stores"


_PLANNED_STORE_SCHEMA_ID = "docspec-planned-store-reference/1.0"


class _DistinctTextIndex:
    """Disk-backed exact membership check with bounded process memory."""

    def __init__(self, directory: Path | None, *, label: str) -> None:
        self._directory = directory
        self._label = label
        self._path: Path | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> _DistinctTextIndex:
        descriptor, name = tempfile.mkstemp(prefix="distinct-", suffix=".sqlite3", dir=self._directory)
        os.close(descriptor)
        self._path = Path(name)
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA cache_size = -2048")
        self._connection.execute("CREATE TABLE members (value TEXT PRIMARY KEY) WITHOUT ROWID")
        return self

    def add(self, value: str) -> None:
        require_text(value, self._label)
        assert self._connection is not None
        try:
            self._connection.execute("INSERT INTO members(value) VALUES (?)", (value,))
        except sqlite3.IntegrityError as error:
            raise IntegrityError(f"{self._label} repeats {value!r}") from error

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._connection is not None:
            self._connection.close()
        if self._path is not None:
            self._path.unlink(missing_ok=True)


class LocalDocumentStoreRepository:
    """Save every DocumentStore transition as one immutable canonical revision."""

    def __init__(
        self,
        root: Path,
        *,
        max_revision_bytes: int = 64 * 1024**2,
        max_inline_bytes: int = 1024**2,
        max_plan_ledger_bytes: int = 4 * 1024**3,
        max_plan_record_bytes: int = 64 * 1024,
        max_plan_store_count: int = 10_000_000,
        verification_scratch: Path | None = None,
    ) -> None:
        if min(
            max_revision_bytes,
            max_inline_bytes,
            max_plan_ledger_bytes,
            max_plan_record_bytes,
            max_plan_store_count,
        ) <= 0:
            raise ValueError("document store byte limits must be positive")
        if max_inline_bytes > max_revision_bytes:
            raise ValueError("max_inline_bytes must not exceed max_revision_bytes")
        self.root = _storage_root(root)
        self.max_revision_bytes = max_revision_bytes
        self.max_inline_bytes = max_inline_bytes
        self.max_plan_ledger_bytes = max_plan_ledger_bytes
        self.max_plan_record_bytes = max_plan_record_bytes
        self.max_plan_store_count = max_plan_store_count
        self._plan_staging = _contained(self.root, ".staging/plans/placeholder", create_parents=True).parent
        self._verification_scratch = self._external_verification_scratch(verification_scratch)

    def _external_verification_scratch(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        path = Path(path)
        if path.is_symlink():
            raise IntegrityError("verification scratch must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return resolved
        raise IntegrityError("verification scratch must be outside the document-store root")

    @staticmethod
    def _store_key(store_id: str) -> str:
        require_text(store_id, "store_id")
        return hashlib.sha256(store_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry_member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest, "document store entry-member digest").removeprefix("sha256:")
        return f"document-store-members/sha256/{hexadecimal[:2]}/{hexadecimal}.jsonl"

    @staticmethod
    def _plan_key(plan_id: str) -> str:
        require_text(plan_id, "plan_id")
        return hashlib.sha256(plan_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _planned_member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest, "planned-store member digest").removeprefix("sha256:")
        return f"planned-store-members/sha256/{hexadecimal[:2]}/{hexadecimal}.jsonl"

    @classmethod
    def _planned_ledger_locator(cls, plan_id: str) -> str:
        return f"planned-store-ledgers/{cls._plan_key(plan_id)}/ledger.json"

    def _saved_payload(self, store: DocumentStore) -> bytes:
        document_store_payload = canonical_json_file_bytes(store.to_dict())
        if len(document_store_payload) > self.max_revision_bytes:
            raise LimitExceededError(f"document store revision exceeds the {self.max_revision_bytes}-byte limit")
        if len(document_store_payload) <= self.max_inline_bytes:
            return document_store_payload

        entry_payload = b"".join(canonical_json_bytes(entry.to_dict()) + b"\n" for entry in store.entries)
        if len(entry_payload) > self.max_revision_bytes:
            raise LimitExceededError(f"document store entry ledger exceeds the {self.max_revision_bytes}-byte limit")
        entry_digest = sha256_digest(entry_payload)
        entry_locator = self._entry_member_locator(entry_digest)
        header = store.to_dict()
        del header["entries"]
        root = {
            "format": "docspec-saved-document-store",
            "formatVersion": "1.0",
            "storeId": store.store_id,
            "revision": store.revision,
            "documentStoreDigest": sha256_digest(document_store_payload),
            "documentStore": header,
            "entriesMember": {
                "path": entry_locator,
                "mediaType": "application/x-ndjson",
                "byteSize": len(entry_payload),
                "digest": entry_digest,
                "recordCount": len(store.entries),
                "schemaId": "docspec-document-store-entry/1.0",
            },
        }
        root_payload = canonical_json_file_bytes(root)
        if len(root_payload) > self.max_inline_bytes:
            raise LimitExceededError(f"document store root exceeds the {self.max_inline_bytes}-byte inline limit")
        _write_once(self.root, entry_locator, entry_payload)
        return root_payload

    def save(self, store: DocumentStore) -> StoreRef:
        payload = self._saved_payload(store)
        digest = sha256_digest(payload)
        locator = f"document-stores/{self._store_key(store.store_id)}/revisions/{store.revision:020d}.json"
        try:
            _write_once(self.root, locator, payload, staging_locator=".staging/writes")
        except IntegrityError as error:
            raise StateTransitionError(
                f"store {store.store_id} revision {store.revision} already has different immutable content"
            ) from error
        return StoreRef(store.store_id, store.revision, locator, digest)

    def load(self, reference: StoreRef) -> DocumentStore:
        path = _contained(self.root, reference.locator)
        if path.is_file() and path.stat().st_size > self.max_revision_bytes:
            raise LimitExceededError(f"document store revision exceeds the {self.max_revision_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("document store bytes differ from their reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.store_id))
        if not isinstance(value, dict):
            raise IntegrityError("document store root must be a JSON object")
        if value.get("format") == "docspec-saved-document-store":
            value = self._expand_saved_root(value, reference)
        try:
            store = DocumentStore.from_dict(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document store record is invalid: {error}") from error
        if store.store_id != reference.store_id or store.revision != reference.revision:
            raise IntegrityError("document store identity or revision differs from its reference")
        expected = f"document-stores/{self._store_key(store.store_id)}/revisions/{store.revision:020d}.json"
        if reference.locator != expected:
            raise IntegrityError("document store locator differs from its identity and revision")
        return store

    def _expand_saved_root(self, root: dict[str, Any], reference: StoreRef) -> dict[str, Any]:
        expected = {
            "format",
            "formatVersion",
            "storeId",
            "revision",
            "documentStoreDigest",
            "documentStore",
            "entriesMember",
        }
        if set(root) != expected or root["formatVersion"] != "1.0":
            raise IntegrityError("saved document store root has an unknown format or invalid closed shape")
        if root["storeId"] != reference.store_id or root["revision"] != reference.revision:
            raise IntegrityError("saved document store root identity differs from its reference")
        require_sha256(root["documentStoreDigest"], "saved document store digest")
        header = root["documentStore"]
        if not isinstance(header, dict) or "entries" in header:
            raise IntegrityError("saved document store header has an invalid shape")
        member = root["entriesMember"]
        if not isinstance(member, dict):
            raise IntegrityError("saved document store entry member has an invalid shape")
        member_size = member.get("byteSize")
        if not isinstance(member_size, int) or isinstance(member_size, bool) or member_size < 0:
            raise IntegrityError("saved document store entry-member size is invalid")
        if member_size > self.max_revision_bytes:
            raise LimitExceededError(
                f"document store entry ledger exceeds the {self.max_revision_bytes}-byte limit"
            )
        path = _verified_member_path(
            self.root,
            member,
            media_type="application/x-ndjson",
            schema_id="docspec-document-store-entry/1.0",
        )
        if member["path"] != self._entry_member_locator(member["digest"]):
            raise IntegrityError("document store entry-member locator differs from its digest")
        entries: list[dict[str, Any]] = []
        for value in _iter_canonical_json_lines(
            path,
            label="document store entry ledger",
            max_line_bytes=self.max_revision_bytes,
        ):
            try:
                entry = DocumentEntry.from_dict(value)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"document store entry ledger is invalid: {error}") from error
            entries.append(entry.to_dict())
        if len(entries) != member["recordCount"]:
            raise IntegrityError("document store entry count differs from its member")
        expanded = {**header, "entries": entries}
        if sha256_digest(canonical_json_file_bytes(expanded)) != root["documentStoreDigest"]:
            raise IntegrityError("expanded document store differs from its declared digest")
        return expanded

    def revisions(self, store_id: str) -> tuple[StoreRef, ...]:
        key = self._store_key(store_id)
        directory = _contained(self.root, f"document-stores/{key}/revisions/placeholder").parent
        if not directory.exists():
            return ()
        if directory.is_symlink() or not directory.is_dir():
            raise IntegrityError("document store revision path is not a regular directory")
        references: list[StoreRef] = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9]{20}\.json", path.name):
                raise IntegrityError("document store revision directory contains an undeclared member")
            revision = int(path.stem)
            payload = path.read_bytes()
            reference = StoreRef(
                store_id,
                revision,
                path.relative_to(self.root).as_posix(),
                sha256_digest(payload),
            )
            self.load(reference)
            references.append(reference)
        return tuple(references)

    def _latest_revision_path(self, store_id: str) -> Path | None:
        key = self._store_key(store_id)
        directory = _contained(self.root, f"document-stores/{key}/revisions/placeholder").parent
        if not directory.exists():
            return None
        if directory.is_symlink() or not directory.is_dir():
            raise IntegrityError("document store revision path is not a regular directory")
        latest_path: Path | None = None
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9]{20}\.json", path.name):
                raise IntegrityError("document store revision directory contains an undeclared member")
            if latest_path is None or path.name > latest_path.name:
                latest_path = path
        return latest_path

    def latest_with_observed_at(self, store_id: str) -> tuple[StoreRef, float] | None:
        """Return a store's latest saved revision and its filesystem write time.

        The write time is a liveness read only: the local clock's epoch-second
        reading of when this repository last wrote a checkpoint or seal for the
        store. No domain object carries a comparable field -- every timestamp a
        run persists (``CapturedFile.acquired_at``, a ``DeliveryReceipt``'s
        ``completed_at``) is the run request's single fixed ``completedAt``
        value, held constant across an entire run for reproducibility, so it
        cannot answer "how long has this store gone untouched". Filesystem
        mtime is the only wall-clock signal this repository actually has, and
        it is read here, not written or sealed anywhere.

        Bounded by one directory listing over that one store's own revision
        count; independent of how many other stores this repository holds.
        """

        latest_path = self._latest_revision_path(store_id)
        if latest_path is None:
            return None
        payload = latest_path.read_bytes()
        reference = StoreRef(
            store_id,
            int(latest_path.stem),
            latest_path.relative_to(self.root).as_posix(),
            sha256_digest(payload),
        )
        self.load(reference)
        return reference, latest_path.stat().st_mtime

    def latest(self, store_id: str) -> StoreRef | None:
        resolved = self.latest_with_observed_at(store_id)
        return None if resolved is None else resolved[0]

    def has_planned_store_ledger(self, plan_id: str) -> bool:
        """Detect durable planning state without treating corruption as absence."""

        locator = self._planned_ledger_locator(plan_id)
        path = _contained(self.root, locator)
        if path.is_symlink():
            raise IntegrityError("planned-store ledger root must not be a symlink")
        if not path.exists():
            return False
        if not path.is_file():
            raise IntegrityError("planned-store ledger root is not a regular file")
        return True

    def _iter_planned_member(
        self,
        path: Path,
        *,
        plan_id: str,
        expected_count: int,
    ) -> Iterator[StoreRef]:
        count = 0
        with _DistinctTextIndex(self._verification_scratch, label="planned-store ledger store_id") as distinct:
            for row in _iter_canonical_json_lines(
                path,
                label="planned-store ledger",
                max_line_bytes=self.max_plan_record_bytes,
            ):
                if set(row) != {"ordinal", "store"} or row["ordinal"] != count:
                    raise IntegrityError("planned-store ledger ordinals must be complete and ordered")
                try:
                    reference = StoreRef.from_dict(row["store"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(f"planned-store ledger contains an invalid reference: {error}") from error
                if reference.revision != 0:
                    raise IntegrityError("planned-store ledger must contain initial planned revisions")
                distinct.add(reference.store_id)
                store = self.load(reference)
                if store.state != StoreState.PLANNED or store.plan_id != plan_id:
                    raise IntegrityError("planned-store ledger contains a non-planned store or a different plan")
                count += 1
                yield reference
        if count != expected_count:
            raise IntegrityError("planned-store ledger count differs from its root")

    def _planned_root(self, reference: LayerRef) -> tuple[dict[str, Any], Path]:
        if (
            reference.layer_kind != _PLANNED_STORE_LAYER_KIND
            or reference.schema_id != _PLANNED_STORE_SCHEMA_ID
            or reference.profile_id != _DOCUMENT_STORE_PROFILE_ID
        ):
            raise IntegrityError("planned-store ledger reference has an unknown logical profile")
        path = _contained(self.root, reference.state_ref)
        if path.is_file() and path.stat().st_size > self.max_inline_bytes:
            raise LimitExceededError(f"planned-store ledger root exceeds the {self.max_inline_bytes}-byte limit")
        payload = _read_exact(self.root, reference.state_ref)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("planned-store ledger root differs from its reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.layer_id))
        expected = {
            "format",
            "formatVersion",
            "ledgerId",
            "layerKind",
            "schemaId",
            "profileId",
            "planId",
            "orderPolicy",
            "member",
            "recordCount",
            "orderedStoreSetDigest",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["format"] != "docspec-planned-store-ledger"
            or value["formatVersion"] != "1.0"
            or value["layerKind"] != _PLANNED_STORE_LAYER_KIND
            or value["schemaId"] != _PLANNED_STORE_SCHEMA_ID
            or value["profileId"] != _DOCUMENT_STORE_PROFILE_ID
            or value["orderPolicy"] != "planner-emission-order"
        ):
            raise IntegrityError("planned-store ledger root has an unknown format or invalid closed shape")
        if not isinstance(value["recordCount"], int) or isinstance(value["recordCount"], bool) or value["recordCount"] < 0:
            raise IntegrityError("planned-store ledger count must be a non-negative integer")
        if value["recordCount"] > self.max_plan_store_count:
            raise LimitExceededError(f"planned-store ledger exceeds the {self.max_plan_store_count}-store limit")
        require_sha256(value["orderedStoreSetDigest"], "planned-store ordered-set digest")
        content = {
            "layerKind": value["layerKind"],
            "schemaId": value["schemaId"],
            "profileId": value["profileId"],
            "planId": value["planId"],
            "orderPolicy": value["orderPolicy"],
            "member": value["member"],
            "recordCount": value["recordCount"],
            "orderedStoreSetDigest": value["orderedStoreSetDigest"],
        }
        if value["ledgerId"] != stable_urn("planned-store-ledger", content):
            raise IntegrityError("planned-store ledger identity differs from its content")
        if (
            reference.layer_id != value["ledgerId"]
            or reference.state_ref != self._planned_ledger_locator(value["planId"])
            or reference.record_count != value["recordCount"]
        ):
            raise IntegrityError("planned-store ledger root differs from its reference fields")
        member = value["member"]
        if not isinstance(member, dict):
            raise IntegrityError("planned-store ledger member has an invalid shape")
        member_size = member.get("byteSize")
        if not isinstance(member_size, int) or isinstance(member_size, bool) or member_size < 0:
            raise IntegrityError("planned-store ledger member size is invalid")
        if member_size > self.max_plan_ledger_bytes:
            raise LimitExceededError(
                f"planned-store ledger member exceeds the {self.max_plan_ledger_bytes}-byte limit"
            )
        member_path = _verified_member_path(
            self.root,
            member,
            media_type="application/x-ndjson",
            schema_id=_PLANNED_STORE_SCHEMA_ID,
        )
        if member["path"] != self._planned_member_locator(member["digest"]):
            raise IntegrityError("planned-store member locator differs from its digest")
        if member["recordCount"] != value["recordCount"]:
            raise IntegrityError("planned-store member count differs from its root")
        return value, member_path

    def seal_planned_stores(self, plan_id: str, references: Iterable[StoreRef]) -> LayerRef:
        """Persist one complete ordered job population without retaining it in memory."""

        require_text(plan_id, "plan_id")
        descriptor, temporary_name = tempfile.mkstemp(prefix="planned-stores-", dir=self._plan_staging)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_count = 0
        store_count = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                with _DistinctTextIndex(self._plan_staging, label="planned-store population store_id") as distinct:
                    for reference in references:
                        if store_count >= self.max_plan_store_count:
                            raise LimitExceededError(
                                f"planned-store ledger exceeds the {self.max_plan_store_count}-store limit"
                            )
                        if reference.revision != 0:
                            raise IntegrityError("only initial planned revisions may enter a planned-store ledger")
                        distinct.add(reference.store_id)
                        store = self.load(reference)
                        if store.state != StoreState.PLANNED or store.plan_id != plan_id:
                            raise IntegrityError("planned-store population contains a non-planned store or another plan")
                        line = canonical_json_bytes({"ordinal": store_count, "store": reference.to_dict()}) + b"\n"
                        if len(line) > self.max_plan_record_bytes:
                            raise LimitExceededError(
                                f"planned-store record exceeds the {self.max_plan_record_bytes}-byte limit"
                            )
                        byte_count += len(line)
                        if byte_count > self.max_plan_ledger_bytes:
                            raise LimitExceededError(
                                f"planned-store ledger exceeds the {self.max_plan_ledger_bytes}-byte limit"
                            )
                        handle.write(line)
                        digest.update(line)
                        store_count += 1
                handle.flush()
                os.fsync(handle.fileno())

            member_digest = f"sha256:{digest.hexdigest()}"
            member_locator = self._planned_member_locator(member_digest)
            member = {
                "path": member_locator,
                "mediaType": "application/x-ndjson",
                "byteSize": byte_count,
                "digest": member_digest,
                "recordCount": store_count,
                "schemaId": _PLANNED_STORE_SCHEMA_ID,
            }
            destination = _contained(self.root, member_locator, create_parents=True)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            _verified_member_path(
                self.root,
                member,
                media_type="application/x-ndjson",
                schema_id=_PLANNED_STORE_SCHEMA_ID,
            )
            ordered_digest = ordered_json_sequence_digest(
                reference.to_dict()
                for reference in self._iter_planned_member(
                    temporary,
                    plan_id=plan_id,
                    expected_count=store_count,
                )
            )
            content = {
                "layerKind": _PLANNED_STORE_LAYER_KIND,
                "schemaId": _PLANNED_STORE_SCHEMA_ID,
                "profileId": _DOCUMENT_STORE_PROFILE_ID,
                "planId": plan_id,
                "orderPolicy": "planner-emission-order",
                "member": member,
                "recordCount": store_count,
                "orderedStoreSetDigest": ordered_digest,
            }
            ledger_id = stable_urn("planned-store-ledger", content)
            root = {
                "format": "docspec-planned-store-ledger",
                "formatVersion": "1.0",
                "ledgerId": ledger_id,
                **content,
            }
            payload = canonical_json_file_bytes(root)
            if len(payload) > self.max_inline_bytes:
                raise LimitExceededError(f"planned-store ledger root exceeds the {self.max_inline_bytes}-byte limit")
            locator = self._planned_ledger_locator(plan_id)
            try:
                _write_once(self.root, locator, payload)
            except IntegrityError as error:
                raise StateTransitionError("plan already has a different immutable store population") from error
            return LayerRef(
                ledger_id,
                _PLANNED_STORE_LAYER_KIND,
                _PLANNED_STORE_SCHEMA_ID,
                _DOCUMENT_STORE_PROFILE_ID,
                locator,
                sha256_digest(payload),
                store_count,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def planned_store_ledger(self, plan_id: str) -> LayerRef:
        locator = self._planned_ledger_locator(plan_id)
        path = _contained(self.root, locator)
        if path.is_file() and path.stat().st_size > self.max_inline_bytes:
            raise LimitExceededError(f"planned-store ledger root exceeds the {self.max_inline_bytes}-byte limit")
        payload = _read_exact(self.root, locator)
        value = thaw_json(parse_canonical_json(payload, label=f"planned-store ledger for {plan_id}"))
        if not isinstance(value, dict):
            raise IntegrityError("planned-store ledger root must be a JSON object")
        try:
            reference = LayerRef(
                value["ledgerId"],
                value["layerKind"],
                value["schemaId"],
                value["profileId"],
                locator,
                sha256_digest(payload),
                value["recordCount"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"planned-store ledger root is invalid: {error}") from error
        self.verify_planned_store_ledger(reference)
        return reference

    def verify_planned_store_ledger(self, reference: LayerRef) -> None:
        root, member_path = self._planned_root(reference)
        count = 0

        def stores() -> Iterator[dict[str, Any]]:
            nonlocal count
            for store_reference in self._iter_planned_member(
                member_path,
                plan_id=root["planId"],
                expected_count=root["recordCount"],
            ):
                count += 1
                yield store_reference.to_dict()

        if ordered_json_sequence_digest(stores()) != root["orderedStoreSetDigest"]:
            raise IntegrityError("planned-store ledger order digest differs from its member")
        if count != root["recordCount"] or count != reference.record_count:
            raise IntegrityError("planned-store ledger count differs")

    def stream_planned_stores(self, reference: LayerRef) -> Iterator[StoreRef]:
        root, member_path = self._planned_root(reference)
        yield from self._iter_planned_member(
            member_path,
            plan_id=root["planId"],
            expected_count=root["recordCount"],
        )
