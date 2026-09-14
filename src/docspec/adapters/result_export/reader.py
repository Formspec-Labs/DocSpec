"""Read a pinned selected Core snapshot without its original workspace."""

from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
import sqlite3
from itertools import islice

from rulespec_artifacts import (ArtifactVerificationError, LocalMemberSource, MemberDescriptor,
    MemberSourceError, admit_artifact, iter_member_descriptors)

from docspec.adapters.storage.core_states import CoreStateStorage
from docspec.adapters.storage.core_selections import CoreSelectionStorage
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.adapters.storage.records import LocalParquetRecordStorage
from docspec.application.core_publication import CorePublisher
from docspec.domain.identity import decode_canonical_json_value, identity_digest
from docspec.domain.core_admission import record_value
from docspec.domain.references import BlobRef
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.core_ledger import MetadataBatch
from .io import ROOT_BYTES, RECORD_BYTES, MANIFEST_BYTES, MANIFEST_KEY, INDEX_KEY, require_limit, scratch, verified_open, read_mapping


class _ExportBlobs:
    def __init__(self, view):
        self.view = view
    def stat(self, reference):
        self.view._reference(reference)
        return reference
    def ensure_ready(self, reference):
        with self.view.open_blob(reference):
            pass
    def read(self, reference, *, max_bytes=None, chunk_size=256 * 1024):
        if max_bytes is not None and reference.byte_size > max_bytes:
            raise LimitExceededError("export blob exceeds max_bytes")
        with self.view.open_blob(reference) as stream:
            while data := stream.read(chunk_size or 256 * 1024):
                yield data


class AdmittedResultExport:
    """Read-only selected state, exact content references and original Core records."""
    def __init__(self, source, resources, index):
        self._source, self._resources, self._index = source, resources, index
        self._closed = False
    def __enter__(self):
        self._require_open()
        return self
    def __exit__(self, *_):
        self.close()
    def close(self):
        self._closed = True
        self._resources.close()
    def _require_open(self):
        if self._closed:
            raise RuntimeError("result export view is closed")
    def _member(self, key):
        self._require_open()
        value = self._index.lookup_record("members", key)
        if value is None:
            raise IntegrityError("export is missing a referenced member")
        return MemberDescriptor.from_dict(value, path="export/member")
    def _verify(self, key):
        with verified_open(self._source, self._member(key)):
            pass
    def _reference(self, reference):
        self._require_open()
        value = self._index.lookup_record("references", identity_digest(reference.to_dict()))
        if value != reference.to_dict():
            raise IntegrityError("reference does not belong to this admitted export")
        member = self._member("blobs/" + reference.locator)
        if (member.byte_size, member.sha256) != (reference.byte_size, reference.digest):
            raise IntegrityError("export reference differs from its member descriptor")
        return member
    @contextmanager
    def open_blob(self, reference):
        with verified_open(self._source, self._reference(reference)) as stream:
            yield stream
    def read_blob(self, reference, *, max_bytes):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if reference.byte_size > max_bytes:
            raise LimitExceededError("export blob exceeds max_bytes")
        with self.open_blob(reference) as stream:
            return stream.read(max_bytes + 1)
    @property
    def pin(self):
        self._require_open()
        return self._pin
    @property
    def summary(self):
        self._require_open()
        return dict(self._summary)
    def record(self, kind, identity):
        self._verify("ledger.sqlite")
        with owned_iterator(self._ledger.read_records([(kind, identity)])) as batches:
            row = next(batches)[0]
        return None if row is None else row.value
    def roots(self):
        self._require_open()
        with verified_open(self._source, self._member("roots.jsonl")) as stream:
            while payload := stream.readline(RECORD_BYTES + 1):
                if len(payload) > RECORD_BYTES:
                    raise LimitExceededError("export root key exceeds its byte limit")
                yield tuple(decode_canonical_json_value(payload.removesuffix(b"\n"), label="export root key"))
    def rows(self, state_id=None):
        self._verify("ledger.sqlite")
        state_id = self._state_id if state_id is None else state_id
        with self._publisher.session() as session:
            manifest = self._states.manifest(session, state_id)
            for reference in self._states._references(manifest).values():
                for physical in self._records.physical_references(reference):
                    self._verify("records/" + physical.locator)
            yield from self._states.rows(session, state_id)

    def _admit(self, artifact, source, *, producer):
        root, spec = artifact.root, artifact.root["spec"]
        if (root["kind"] != "docspec-core-export" or root["producer"] != producer.as_dict()
                or set(spec) != {"stateId", "schemaId"} or spec["schemaId"] != "urn:docspec:core-export:1"):
            raise IntegrityError("export kind, producer or schema is invalid")
        if len(artifact.manifests) != 1 or (artifact.manifests[0].scope_kind, artifact.manifests[0].scope_id,
                artifact.manifests[0].object_key) != ("global", "selected-core-state", MANIFEST_KEY):
            raise IntegrityError("export has an invalid member manifest")
        with owned_iterator(iter_member_descriptors(artifact, source)) as descriptors:
            for member in descriptors:
                key = member.object_key
                valid = ((key == INDEX_KEY and member.role == "index") or (key == "ledger.sqlite" and member.role == "metadata")
                    or (key == "references.jsonl" and member.role == "references") or (key == "roots.jsonl" and member.role == "roots") or (key and key.startswith("records/") and member.role == "records")
                    or (key and key.startswith("blobs/") and member.role == "blobs"))
                if not valid:
                    raise IntegrityError("export contains an unsupported member")
                self._index.add_record("members", identity=key, source_item_id=key, record=member.as_dict())
        index = read_mapping(source, self._member(INDEX_KEY), max_bytes=ROOT_BYTES)
        if index != {"format": "docspec-core-export", "version": 1, "state_id": spec["stateId"]}:
            raise IntegrityError("export index differs from its selected state")
        with verified_open(source, self._member("references.jsonl")) as stream:
            while payload := stream.readline(RECORD_BYTES + 1):
                if len(payload) > RECORD_BYTES:
                    raise LimitExceededError("export reference exceeds its byte limit")
                value = decode_canonical_json_value(payload.removesuffix(b"\n"), label="export reference")
                reference = BlobRef.from_dict(value)
                self._index.add_record("references", identity=identity_digest(value), source_item_id=reference.locator, record=value)
                self._reference(reference)
        path = source.root if hasattr(source, "root") else self._path
        self._records = self._resources.enter_context(closing(LocalParquetRecordStorage(path / "records", create=False)))
        self._ledger = self._resources.enter_context(closing(LocalSqliteCoreLedger(path / "ledger.sqlite", read_only=True, record_storage=self._records)))
        self._ledger.verify_snapshot()
        self._states = CoreStateStorage(self._records)
        self._publisher = CorePublisher(self._ledger, _ExportBlobs(self), states=self._states,
            selections=CoreSelectionStorage(self._records, self._states))
        self._state_id = spec["stateId"]
        root_count, selected_state_seen = 0, False
        with self._publisher.session() as session:
            with self._ledger._transaction() as connection:
                keys = connection.execute("SELECT kind,record_id FROM records WHERE kind='state_representation'")
                for batch in self._ledger.read_records(keys):
                    for row in batch:
                        if row.available:
                            self._states.check_representation(session, record_value(row.value), retained=False, publish_entities=False)
            with owned_iterator(self.roots()) as selected:
                while group := tuple(islice(selected, 16)):
                    root_count += len(group)
                    selected_state_seen |= ("state", self._state_id) in group
                    session.validate(MetadataBatch("export-admission", retained=group))
        if not selected_state_seen:
            raise IntegrityError("export roots omit the selected state")
        self._pin = artifact.pin
        self._summary = {"stateId": self._state_id, "memberCount": artifact.member_count,
                         "embeddedPayloadBytes": artifact.total_member_byte_size, "rootCount": root_count,
                         "scope": "selected-state-and-explicit-root-evidence"}


def open_result_export(path, *, expected_pin, producer, max_output_bytes):
    require_limit(max_output_bytes)
    resources = ExitStack()
    view = None
    try:
        source = LocalMemberSource(Path(path))
        with source.open("artifact.json") as stream:
            payload = stream.read(ROOT_BYTES + 1)
        if len(payload) > ROOT_BYTES:
            raise LimitExceededError("export root exceeds its byte limit")
        root = decode_canonical_json_value(payload, label="result export root")
        sizes = [root["counts"]["totalMemberByteSize"], *(value["byteSize"] for value in root["memberManifests"]),
                 *(value["totalMemberByteSize"] for value in root["memberManifests"])]
        if any(type(size) is not int or size < 0 for size in sizes):
            raise IntegrityError("export root declares invalid byte counts")
        total = len(payload) + sum(value["byteSize"] for value in root["memberManifests"]) + max(
            root["counts"]["totalMemberByteSize"], sum(value["totalMemberByteSize"] for value in root["memberManifests"]))
        if total > max_output_bytes:
            raise LimitExceededError("result export exceeds max_output_bytes")
        index = resources.enter_context(scratch(4 * max_output_bytes))
        view = AdmittedResultExport(source, resources, index)
        view._path = Path(path)
        admit_artifact(source, expected_pin=expected_pin, root_byte_limit=ROOT_BYTES,
            manifest_byte_limit=min(MANIFEST_BYTES, max_output_bytes),
            semantic_verifier=lambda artifact, member_source: view._admit(artifact, member_source, producer=producer))
        return view
    except BaseException as error:
        resources.close()
        if isinstance(error, (ArtifactVerificationError, MemberSourceError, KeyError, TypeError, ValueError, sqlite3.Error)):
            raise IntegrityError(f"result export is invalid: {error}") from error
        raise
