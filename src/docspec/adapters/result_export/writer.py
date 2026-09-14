"""Publish a selected Core state and its required retained evidence atomically."""

from contextlib import ExitStack, closing
from pathlib import Path
from itertools import chain, islice
import os
import shutil
import sqlite3
import tempfile

from rulespec_artifacts import (ArtifactPin, LocalMemberSource, MemberDescriptor, Producer,
    build_artifact_root, describe_member, publish_directory_no_replace, write_member_manifest)

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.core_states import CoreStateStorage
from docspec.adapters.storage.core_selections import CoreSelectionStorage
from docspec.adapters.storage.files import _contained
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.adapters.storage.records import LocalParquetRecordStorage
from docspec.application.core_maintenance import CoreMaintenance
from docspec.application.core_publication import CorePublisher
from docspec.domain.identity import canonical_value_bytes, identity_digest
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.core_ledger import MetadataBatch
from .io import ROOT_BYTES, MANIFEST_BYTES, MANIFEST_KEY, INDEX_KEY, require_limit, scratch, verified_open


class _CopyBlobs:
    """During assembly, shared content checks copy exactly the referenced bytes."""
    def __init__(self, source, destination, index, charge):
        self.source, self.destination, self.index, self.charge = source, destination, index, charge
    def _remember(self, reference):
        key = identity_digest(reference.to_dict())
        if self.index.lookup_record("references", key) is None:
            self.index.add_record("references", identity=key, source_item_id=reference.locator, record=reference.to_dict())
    def copy(self, reference):
        self._remember(reference)
        key = "blobs/" + reference.locator
        if self.index.lookup_record("copied", key) is None:
            self.charge(reference.byte_size)
            with owned_iterator(self.source.read(reference)) as chunks:
                copied = self.destination.put_if_absent(chunks, media_type=reference.media_type,
                    expected_digest=reference.digest, expected_size=reference.byte_size)
            if copied != reference:
                raise IntegrityError("export requires a portable content-addressed locator")
            self.index.add_record("copied", identity=key, source_item_id=key, record={"key": key})
        return reference
    def put_if_absent(self, chunks, **kwargs):
        reference = self.destination.put_if_absent(chunks, **kwargs)
        self._remember(reference)
        key = "blobs/" + reference.locator
        if self.index.lookup_record("copied", key) is None:
            self.charge(reference.byte_size)
            self.index.add_record("copied", identity=key, source_item_id=key, record={"key": key})
        return reference
    def stat(self, reference):
        return self.copy(reference)
    def ensure_ready(self, reference):
        self.copy(reference)
        self.destination.ensure_ready(reference)
    def read(self, reference, **kwargs):
        self.copy(reference)
        yield from self.destination.read(reference, **kwargs)


def export_result(publisher, records, state_id, destination, *, producer: Producer, max_output_bytes: int, additional_roots=()) -> ArtifactPin:
    """Export the complete selected state, regardless of which work was reused."""
    from .reader import open_result_export
    require_limit(max_output_bytes)
    Producer.from_dict(producer.as_dict(), path="export/producer")
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise IntegrityError("export destination must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    working = Path(tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent))
    try:
        with ExitStack() as stack:
            index = stack.enter_context(scratch(4 * max_output_bytes))
            source_session = stack.enter_context(publisher.session())
            def keys(collection="scope"):
                with owned_iterator(index.stream_records(collection)) as values:
                    for value in values:
                        yield tuple(value["key"])
            with owned_iterator(additional_roots) as extra_roots, owned_iterator(chain([("state", state_id)], extra_roots)) as roots:
                while group := tuple(islice(roots, 16)):
                    for key in group:
                        identity = identity_digest(list(key))
                        if index.lookup_record("roots", identity) is None:
                            index.add_record("roots", identity=identity, source_item_id=identity, record={"key": list(key)})
                    required, full = source_session.retention_scope(MetadataBatch("export-check", retained=group))
                    for collection, selected in (("scope", required), ("full", full)):
                        for key in selected:
                            identity = identity_digest(list(key))
                            if index.lookup_record(collection, identity) is None:
                                index.add_record(collection, identity=identity, source_item_id=identity, record={"key": list(key)})
            def metadata_keys():
                for key in keys():
                    if key[0] != "state_representation" or index.lookup_record("roots", identity_digest(list(key))) is not None:
                        yield key
            publisher.ledger.export_snapshot(working / "ledger.sqlite", metadata_keys(), full=keys("full"))
            target_records = stack.enter_context(closing(LocalParquetRecordStorage(working / "records")))
            ledger = stack.enter_context(closing(LocalSqliteCoreLedger(working / "ledger.sqlite", record_storage=target_records)))
            total = 0
            def charge(size):
                nonlocal total
                total += size
                if total > max_output_bytes:
                    raise LimitExceededError("result export exceeds max_output_bytes")
            blobs = _CopyBlobs(publisher.blobs, LocalContentAddressedBlobStore(working / "blobs"), index, charge)
            states = CoreStateStorage(target_records)
            target = CorePublisher(ledger, blobs, states=states, selections=CoreSelectionStorage(target_records, states))
            source_maintenance = CoreMaintenance(publisher, records)
            scanned = set()
            class CopyContent:
                def add(self, content, *, protected):
                    reference = content.reference
                    if content.store == "blobs":
                        blobs.copy(reference)
                    else:
                        key = "records/" + reference.locator
                        if index.lookup_record("copied", key) is None:
                            charge(reference.byte_size)
                            member = MemberDescriptor(object_key=reference.locator, role="records", media_type=reference.media_type,
                                byte_size=reference.byte_size, sha256=reference.digest)
                            path = _contained(working, key, create_parents=True)
                            with verified_open(LocalMemberSource(records.root), member) as source, path.open("xb") as output:
                                shutil.copyfileobj(source, output, length=256 * 1024)
                            index.add_record("copied", identity=key, source_item_id=key, record={"key": key})
            # Scalar content and selected values keep their exact retained form.
            # Full states are repacked below, omitting unused entity-layer rows.
            for batch in publisher.ledger.read_records(metadata_keys()):
                for row in batch:
                    if row is not None and row.available and index.lookup_record("full", identity_digest(list(row.key))) is not None:
                        source_maintenance.inventory_record(CopyContent(), row.value, protected=True, scanned=scanned, entity_targets=set())
            source_maintenance.inventory_recovery(CopyContent(), execution_ids=(identity for kind, identity in keys() if kind == "execution"))
            with target.session() as session:
                for kind, identity in keys("full"):
                    if kind == "state":
                        row = next(source_session.read_records([(kind, identity)]))[0]
                        if row.available:
                            states.create_keyed(session, state_id=identity, representation_id="export:" + identity_digest(identity),
                                unit_id="export-state:" + identity_digest(identity), rows=source_session.states.rows(source_session, identity))
                with owned_iterator(keys("roots")) as roots:
                    while group := tuple(islice(roots, 16)):
                        session.validate(MetadataBatch("export-ready", retained=group))
            # SQL history is copied as data; no write-ahead log or mutable head is exported.
            ledger.close()
            with closing(sqlite3.connect(working / "ledger.sqlite")) as connection:
                if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise IntegrityError("export metadata checkpoint is busy")
                if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise IntegrityError("export metadata remains in write-ahead-log mode")
                connection.execute("VACUUM")
            (working / "ledger.sqlite.content.lock").unlink(missing_ok=True)
            (working / "ledger.sqlite-wal").unlink(missing_ok=True)
            (working / "ledger.sqlite-shm").unlink(missing_ok=True)
            def add_member(key, role, media_type):
                member = describe_member(LocalMemberSource(working), object_key=key, role=role, media_type=media_type)
                index.add_record("members", identity=key, source_item_id=key, record=member.as_dict())
                return member
            # Only immutable files created for this selected scope become members.
            total = 0
            for directory in ("records", "blobs"):
                for path in sorted((working / directory).rglob("*")):
                    if path.is_file():
                        member = add_member(path.relative_to(working).as_posix(), directory, "application/octet-stream")
                        charge(member.byte_size)
            charge(add_member("ledger.sqlite", "metadata", "application/vnd.sqlite3").byte_size)
            references_path = working / "references.jsonl"
            with references_path.open("wb") as output, owned_iterator(index.stream_records("references")) as references:
                for reference in references:
                    payload = canonical_value_bytes(reference) + b"\n"
                    charge(len(payload))
                    output.write(payload)
            add_member("references.jsonl", "references", "application/x-ndjson")
            roots_path = working / "roots.jsonl"
            with roots_path.open("wb") as output, owned_iterator(keys("roots")) as roots:
                for key in roots:
                    payload = canonical_value_bytes(list(key)) + b"\n"
                    charge(len(payload))
                    output.write(payload)
            add_member("roots.jsonl", "roots", "application/x-ndjson")
            payload = canonical_value_bytes({"format": "docspec-core-export", "version": 1, "state_id": state_id})
            (working / INDEX_KEY).write_bytes(payload)
            charge(len(payload))
            add_member(INDEX_KEY, "index", "application/json")
            with (working / MANIFEST_KEY).open("wb") as output, owned_iterator(index.stream_records("members")) as members:
                manifest = write_member_manifest(output, scope_kind="global", scope_id="selected-core-state", object_key=MANIFEST_KEY,
                    members=(MemberDescriptor.from_dict(value, path="export/member") for value in members),
                    byte_limit=min(MANIFEST_BYTES, max_output_bytes - total))
            charge(manifest.byte_size)
            root = build_artifact_root(kind="docspec-core-export", producer=producer,
                spec={"stateId": state_id, "schemaId": "urn:docspec:core-export:1"}, manifests=(manifest,))
            payload = canonical_value_bytes(root)
            if len(payload) > ROOT_BYTES:
                raise LimitExceededError("result export root exceeds its byte limit")
            charge(len(payload))
            (working / "artifact.json").write_bytes(payload)
            pin = ArtifactPin(root["logicalId"], root["artifactDigest"])
            with open_result_export(working, expected_pin=pin, producer=producer, max_output_bytes=max_output_bytes):
                pass
            for path in working.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            try:
                publish_directory_no_replace(working, destination)
            except FileExistsError:
                with open_result_export(destination, expected_pin=pin, producer=producer, max_output_bytes=max_output_bytes):
                    pass
            return pin
    finally:
        if working.exists():
            shutil.rmtree(working)
