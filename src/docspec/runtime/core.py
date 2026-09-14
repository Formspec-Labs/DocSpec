"""One local Core assembly for document and general dataset operations."""

from contextlib import ExitStack, closing
from pathlib import Path

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.core_selections import CoreSelectionStorage
from docspec.adapters.storage.core_states import CoreStateStorage
from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.adapters.storage.records import LocalParquetRecordStorage
from docspec.application.core_execution import CoreOperations
from docspec.application.core_edits import prepare_revision
from docspec.application.core_inspection import inspect_record
from docspec.application.core_maintenance import CoreMaintenance
from docspec.application.core_publication import CorePublisher
from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes, sha256_digest
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_ROWS


class CoreWorkspace:
    def __init__(self, path, *, blobs=None, engine_memory_bytes=ENGINE_MEMORY_BYTES):
        self.path = Path(path).resolve()
        self._resources = ExitStack()
        try:
            self.records = self._resources.enter_context(closing(LocalParquetRecordStorage(
                self.path / "records", engine_memory_bytes=engine_memory_bytes)))
            self.ledger = self._resources.enter_context(closing(LocalSqliteCoreLedger(
                self.path / "ledger.sqlite", record_storage=self.records)))
            self.blobs = blobs if blobs is not None else LocalContentAddressedBlobStore(self.path / "blobs")
            self.states = CoreStateStorage(self.records)
            self.selections = CoreSelectionStorage(self.records, self.states)
            self.publisher = CorePublisher(self.ledger, self.blobs, states=self.states, selections=self.selections)
            self.operations = CoreOperations(self.publisher)
            self.maintenance = CoreMaintenance(self.publisher, self.records)
        except BaseException:
            self.close()
            raise

    def documents(self, *, fetcher, extractor=None, segmenter=None):
        from docspec.application.documents import DocumentPipeline
        return DocumentPipeline(self.publisher, fetcher=fetcher, extractor=extractor, segmenter=segmenter)

    def create(self, state_id, rows):
        """Retain streamed (member key, JSON value) pairs as distinct occurrences."""
        def entities():
            with owned_iterator(rows) as source:
                for key, value in source:
                    identity = "urn:docspec:occurrence:" + sha256_digest(canonical_value_bytes([state_id, key]))
                    yield key, core.Entity(format_version=1, entity_id=identity, entity_type="occurrence", value=core.InlineValue(value=value))
        with self.publisher.session() as session:
            return self.states.create_keyed(session, state_id=state_id, representation_id=state_id + ":physical",
                                            unit_id=state_id + ":import", rows=entities())

    def retain(self, records, *, unit_id, roots):
        with self.publisher.session() as session:
            return session.publish(MetadataBatch(unit_id, records=bounded_items(records, limit=BATCH_ROWS),
                                                 retained=bounded_items(roots, limit=BATCH_ROWS)))

    def revise(self, revision):
        with self.publisher.session() as session:
            return self.operations.publish([prepare_revision(self.operations, revision, session=session)], session=session)[0]

    def inspect(self, kind, identity, *, progress_limit=20):
        with self.publisher.session() as session:
            return inspect_record(session, (kind, identity), progress_limit=progress_limit)

    def compare(self, older, newer, *, sample_limit=20):
        with self.publisher.session() as session:
            return self.states.compare(session, older, newer, sample_limit=sample_limit)

    def export(self, state_id, destination, *, producer, max_output_bytes, additional_roots=()):
        """Export the state and explicitly requested evidence as a pinned artifact."""
        from docspec.adapters.result_export.writer import export_result
        return export_result(self.publisher, self.records, state_id, destination, producer=producer,
                             max_output_bytes=max_output_bytes, additional_roots=additional_roots)

    def rows(self, state_id):
        """Stream admitted occurrences in deterministic key order."""
        with self.publisher.session() as session:
            yield from self.states.rows(session, state_id)

    def close(self):
        self._resources.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
