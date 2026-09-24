"""Immutable Iceberg snapshots, with DuckDB owning all bulk data writes."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, local
from typing import Any
from uuid import uuid4

import duckdb
import pyarrow as pa

from docspec.ports.record_storage import bounded_batches, bounded_rows
from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.streams import owned_iterator
from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES, connect
from docspec.adapters.storage.files import _available_paths, _contained, _read_exact, _storage_root, _write_once, delete_content, sha256_file
from docspec.adapters.storage.iceberg import IcebergCatalog, recovery_references, identifier, literal, seal_snapshot, snapshot, snapshot_data_files, snapshot_files
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, parse_canonical_json, require_text, sha256_digest, stable_urn, thaw_json
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket, record_key
from docspec.errors import IntegrityError, LimitExceededError

_PROFILE_ID = 'urn:docspec:profile:record-storage:iceberg:1'
_ADMITTED_LAYER_LIMIT = 8


def _physical_schema(schema):
    return pa.schema([*ENCODED_RECORD_SCHEMA, *(pa.field(name, pa.string() if kind == 'string' else pa.binary())
                                         for name, kind in schema.columns)])


def _column_list(schema):
    return ', '.join(identifier(name) for name in _physical_schema(schema).names)


class IcebergRecordStorage:
    """Local retained snapshots; only writes require a REST catalog.

    Catalog names are temporary write handles, never authoritative DocSpec heads.
    Each edit forks its base metadata, commits through DuckDB, then pins the result
    before the logical ledger may publish it. Cleanup remains ledger-controlled.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_member_bytes: int = 256 * 1024**2,
        max_record_bytes: int = 8 * 1024**2,
        max_root_bytes: int = 32 * 1024**2,
        max_merge_scratch_bytes: int = 128 * 1024**3,
        engine_memory_bytes: int = ENGINE_MEMORY_BYTES,
        merge_scratch_root: Path | None = None,
        create: bool = True,
        catalog: IcebergCatalog | None = None,
    ) -> None:
        if min(max_member_bytes, max_record_bytes, max_root_bytes, max_merge_scratch_bytes, engine_memory_bytes) <= 0:
            raise ValueError("record storage limits must be positive")
        self.root = _storage_root(root, create=create)
        self.catalog = catalog if catalog is not None else IcebergCatalog.environment()
        self._catalog_client = None
        self.max_member_bytes = max_member_bytes
        self.max_record_bytes = max_record_bytes
        self.max_root_bytes = max_root_bytes
        self.max_merge_scratch_bytes = max_merge_scratch_bytes
        self.engine_memory_bytes = engine_memory_bytes
        self.merge_scratch_root = (
            None if merge_scratch_root is None else _storage_root(merge_scratch_root, create=create)
        )
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._connection_lock = Lock()
        self._admissions = local()

    @contextmanager
    def _cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._connection_lock:
            if self._connection is None:
                scratch = tempfile.TemporaryDirectory(prefix="docspec-record-query-", dir=self.merge_scratch_root)
                try:
                    # This bounds DuckDB's managed memory, not total process RSS.
                    self._connection = connect(scratch.name, memory_bytes=self.engine_memory_bytes,
                                               scratch_bytes=self.max_merge_scratch_bytes)
                except BaseException:
                    scratch.cleanup()
                    raise
                self._connection.execute("INSTALL iceberg")
                self._connection.execute("LOAD iceberg")
                self._scratch = scratch
            cursor = self._connection.cursor()
        try:
            yield cursor
        except duckdb.OutOfMemoryException as error:
            raise LimitExceededError("record query exceeds its native memory allowance") from error
        except duckdb.Error as error:
            raise IntegrityError(f"record Iceberg operation failed: {error}") from error
        finally:
            cursor.close()

    def close(self) -> None:
        """Release native resources after all workers and iterators have stopped."""
        with self._connection_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                self._catalog_client = None
            if self._scratch is not None:
                self._scratch.cleanup()
                self._scratch = None

    @contextmanager
    def admission_scope(self):
        """Reuse checked immutable files only while the caller prevents cleanup.

        Nested scopes share this thread's bounded working set. Explicit audits
        remain fresh, and leaving the outer scope discards every cached handle.
        """
        if getattr(self._admissions, "layers", None) is not None:
            yield
            return
        self._admissions.layers = {}
        try:
            yield
        finally:
            del self._admissions.layers

    def _remember_admitted(self, layer):
        layers = getattr(self._admissions, "layers", None)
        if layers is not None:
            layers.pop(layer.reference, None)
            layers[layer.reference] = layer
            while len(layers) > _ADMITTED_LAYER_LIMIT:
                layers.pop(next(iter(layers)))
        return layer

    def _forget_admitted(self, reference):
        layers = getattr(self._admissions, "layers", None)
        if layers is not None:
            layers.pop(reference, None)

    def admitted(self, reference: LayerRef) -> AdmittedRecordLayer:
        """Use this protection scope's handle, or freshly check availability."""
        layers = getattr(self._admissions, "layers", None)
        if layers is not None and reference in layers:
            return self._remember_admitted(layers[reference])
        return self.available(reference)

    @staticmethod
    def _schema_dict(schema: RecordSchema) -> dict[str, Any]:
        return {
            "schemaId": schema.schema_id, "fields": list(schema.fields),
            "identityField": schema.identity_field, "partitionField": schema.partition_field,
            "columns": [list(column) for column in schema.columns],
        }

    @staticmethod
    def _policy_dict(policy: PartitionPolicy) -> dict[str, Any]:
        return {"policyId": policy.policy_id, "bucketCount": policy.bucket_count}

    def identity_field(self, reference: LayerRef) -> str:
        """Return the verified layer's logical identity field name."""

        return self.schema(reference).identity_field

    def compact(self, base: AdmittedRecordLayer) -> AdmittedRecordLayer:
        """Repack admitted canonical bytes and check exact row equivalence."""
        if base._storage is not self:
            raise IntegrityError("compaction base belongs to another record store")
        with closing(base.batches()) as batches:
            compacted = self.retain_batches(batches, layer_kind=base.reference.layer_kind, schema=base.schema,
                                             partition_policy=base.partition_policy)
        with self.relations({"before": base, "after": compacted}) as relations:
            names = _physical_schema(base.schema).names
            def renamed(prefix):
                return ", ".join('"' + name.replace('"', '""') + '" AS "' + prefix + str(index) + '"'
                                 for index, name in enumerate(names))
            before = relations["before"].project(renamed("old"))
            after = relations["after"].project(renamed("new"))
            mismatch = "old0 IS NULL OR new0 IS NULL OR " + " OR ".join(
                f"old{index} IS DISTINCT FROM new{index}" for index in range(1, len(names)))
            if before.join(after, "old0 = new0", how="outer").filter(mismatch).limit(1).fetchone():
                raise IntegrityError("compaction changed logical records")
        return compacted

    def schema(self, reference: LayerRef) -> RecordSchema:
        """Return the verified layer's logical schema."""

        return self._verified_root(reference)[1]

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy:
        """Return the verified layer's partition policy."""

        return self._verified_root(reference)[2]

    def write_layer(
        self, records: Iterable[Mapping[str, Any]], *, layer_kind: str, schema: RecordSchema,
        partition_policy: PartitionPolicy, base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef:
        """Admit logical rows, then use the same encoded batch writer as bulk work."""
        def rows():
            with owned_iterator(records) as source:
                for record in source:
                    if set(record) != set(schema.fields):
                        raise IntegrityError("record does not match its closed logical schema")
                    if schema.columns:
                        yield tuple(record[name] for name in schema.fields)
                        continue
                    yield (
                        record_key(record[schema.identity_field], schema.identity_field),
                        record_key(record[schema.partition_field], schema.partition_field),
                        canonical_json_bytes(record),
                    )

        return self.write_batches(
            encoded_batches(rows(), _physical_schema(schema), byte_column=tuple(range(len(schema.fields))) if schema.columns else 2, max_value_bytes=self.max_record_bytes),
            layer_kind=layer_kind, schema=schema, partition_policy=partition_policy,
            base=base, replace_partitions=replace_partitions,
        )

    def write_batches(
        self, batches: Iterable[pa.RecordBatch], *, layer_kind: str, schema: RecordSchema,
        partition_policy: PartitionPolicy, base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef:
        """Retain already admitted canonical records from internal bulk operations.

        Input uses record_identity/string, partition_value/string, record_json/binary.
        Callers own logical schema/canonical admission and routing correspondence;
        new external mappings use write_layer. This boundary checks physical shape,
        limits, identity order and partition scope without re-encoding payloads.
        Untouched base references keep their existing physical admission lifetime.
        """
        return self._write_batches(
            batches, layer_kind=layer_kind, schema=schema, partition_policy=partition_policy,
            base=base, replace_partitions=replace_partitions,
        ).reference

    def retain_batches(
        self, batches: Iterable[pa.RecordBatch], *, layer_kind: str, schema: RecordSchema,
        partition_policy: PartitionPolicy, base: AdmittedRecordLayer | None = None,
        replace_partitions: frozenset[int] | None = None,
        ordered: bool = True, target_member_bytes: int | None = None,
    ) -> AdmittedRecordLayer:
        """Return the new write's admission directly, without auditing it again.

        The caller admits incoming logical payloads. An incremental write also
        carries its base's admission, within the same content-protection scope.
        """
        if base is not None and base._storage is not self:
            raise IntegrityError("incremental base belongs to another record store")
        return self._write_batches(
            batches, layer_kind=layer_kind, schema=schema, partition_policy=partition_policy,
            base=base, replace_partitions=replace_partitions,
            ordered=ordered, target_member_bytes=target_member_bytes,
        )

    def _verified_root(self, reference):
        payload = _read_exact(self.root, reference.state_ref, max_bytes=self.max_root_bytes)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError('record layer root differs from its reference')
        root = thaw_json(parse_canonical_json(payload, label='Iceberg record layer'))
        fields = {'format', 'version', 'layerKind', 'schema', 'partitionPolicy', 'metadata', 'integrity', 'recordCount'}
        if not isinstance(root, dict) or set(root) != fields or root['format'] != 'docspec-iceberg-records' or type(root['version']) is not int or root['version'] != 1 or type(root['recordCount']) is not int:
            raise IntegrityError('record layer root has an invalid format')
        try:
            value = root['schema']
            if set(value) != {'schemaId', 'fields', 'identityField', 'partitionField', 'columns'} or set(root['partitionPolicy']) != {'policyId', 'bucketCount'}:
                raise ValueError('invalid schema or partition policy shape')
            schema = RecordSchema(value['schemaId'], tuple(value['fields']), value['identityField'], value['partitionField'],
                                  tuple(tuple(column) for column in value['columns']))
            policy = PartitionPolicy(root['partitionPolicy']['policyId'], root['partitionPolicy']['bucketCount'])
            metadata = BlobRef.from_dict(root['metadata'])
            integrity = BlobRef.from_dict(root['integrity'])
            if integrity.locator != metadata.locator + '.sha256':
                raise ValueError('metadata checksum does not match the retained file')
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError('invalid record layer schema or metadata reference') from error
        expected = LayerRef(stable_urn('record-layer', root), root['layerKind'], schema.schema_id, _PROFILE_ID,
                            f'record-layers/sha256/{reference.digest[7:9]}/{reference.digest[7:]}.json',
                            reference.digest, root['recordCount'])
        if reference != expected or not metadata.locator.startswith('iceberg/'):
            raise IntegrityError('record layer differs from its reference')
        return root, schema, policy

    def available(self, reference):
        """Verify the layer root and recovery files and return its admitted handle."""

        self._forget_admitted(reference)
        root, schema, policy = self._verified_root(reference)
        table = snapshot(self.root, root['metadata'])
        # Publication callers reuse newly written admissions. Fresh readers check
        # all recovery files, including positional deletes and shared manifests.
        refs = recovery_references(self.root, BlobRef.from_dict(root['integrity']))
        _available_paths(self.root, ((ref.locator, ref.byte_size) for ref in refs))
        return self._remember_admitted(AdmittedRecordLayer(self, reference, root, schema, policy, table))

    def verify_members(self, reference):
        """Re-hash every recovery file and compare with this snapshot's file set."""

        self._forget_admitted(reference)
        actual = set()
        for ref in self.physical_references(reference):
            if ref.locator.startswith('iceberg/') and not ref.locator.endswith('.sha256'):
                actual.add(ref.locator)
            if sha256_file(_contained(self.root, ref.locator)) != (ref.digest, ref.byte_size):
                raise IntegrityError('retained Iceberg file differs from its checksum')
        table = snapshot(self.root, self._verified_root(reference)[0]['metadata'])
        if actual != {path.relative_to(self.root).as_posix() for path in snapshot_files(table)}:
            raise IntegrityError('Iceberg checksums differ from the snapshot recovery files')

    def verify(self, reference):
        """Verify member files and the declared record count."""

        self.verify_members(reference)
        if sum(1 for _ in self.stream(reference)) != reference.record_count:
            raise IntegrityError('record count differs from its description')

    def admit(self, reference):
        """Fully verify a layer and return it admitted."""

        self.verify(reference)
        return self.available(reference)

    @contextmanager
    def _relation(self, layer, *, cursor=None, partitions=None, record_ids=None, identity_ranges=None, include_bucket=False):
        if layer._storage is not self:
            raise IntegrityError('admitted layer belongs to another record store')
        if partitions is not None and any(p < 0 or p >= layer.partition_policy.bucket_count for p in partitions):
            raise ValueError('selected partition is outside the layer partition policy')
        path = Path(layer.table.metadata_location)
        with (self._cursor() if cursor is None else nullcontext(cursor)) as cursor:
            # Explicit version plus moved-path resolution reads exactly the pin,
            # never a catalog head, directory glob or version-hint file.
            relation = cursor.sql(f"SELECT * FROM iceberg_scan({literal(path.parent.parent)}, "
                f"version={literal(path.name.removesuffix('.metadata.json'))}, "
                "version_name_format='%s%s.metadata.json', allow_moved_paths=true)")
            if partitions is not None:
                relation = relation.filter(duckdb.ColumnExpression('bucket').isin(*(duckdb.ConstantExpression(p) for p in partitions))) if partitions else relation.filter('false')
            if record_ids is not None:
                relation = relation.filter(duckdb.ColumnExpression('record_identity').isin(*(duckdb.ConstantExpression(k) for k in record_ids))) if record_ids else relation.filter('false')
            if identity_ranges is not None:
                ranges = list(identity_ranges)
                relation = relation.filter(' OR '.join(f'(record_identity BETWEEN {literal(lo)} AND {literal(hi)})' for lo, hi in ranges) or 'false')
            yield relation.project(_column_list(layer.schema) + (', bucket' if include_bucket else ''))

    @contextmanager
    def relations(self, references, *, partitions=None, tables=None, identities=None, identity_ranges=None, cursor=None):
        """Open named native relations over admitted layers and caller-supplied tables."""

        with (self._cursor() if cursor is None else nullcontext(cursor)) as cursor, ExitStack() as stack:
            result = {}
            for name, reference in references.items():
                layer = reference if isinstance(reference, AdmittedRecordLayer) else self.admitted(reference)
                result[name] = stack.enter_context(self._relation(layer, cursor=cursor,
                    partitions=None if partitions is None else partitions.get(name),
                    record_ids=None if identities is None else identities.get(name),
                    identity_ranges=None if identity_ranges is None else identity_ranges.get(name)))
            for name, table in (tables or {}).items():
                if name in result:
                    raise IntegrityError('native input names must be distinct')
                result[name] = cursor.from_arrow(table)
            yield result

    def _rows(self, layer, *, partitions=None, record_id=None, partition_value=None):
        with self._relation(layer, partitions=partitions, record_ids=None if record_id is None else [record_id], include_bucket=True) as relation:
            if partition_value is not None:
                relation = relation.filter(duckdb.ColumnExpression('partition_value') == duckdb.ConstantExpression(partition_value))
            previous = None
            with closing(relation.order('record_identity').to_arrow_reader(256)) as reader:
                for batch in bounded_batches(reader, byte_column='record_json', max_value_bytes=self.max_record_bytes):
                    for physical in batch.to_pylist():
                        payload = physical['record_json']
                        if not isinstance(payload, bytes):
                            raise IntegrityError('record member payload must be binary')
                        record = ({field: physical[field] for field in layer.schema.fields} if layer.schema.columns else
                                  thaw_json(parse_canonical_json(payload, label='record member', file_form=False)))
                        if not isinstance(record, dict) or set(record) != set(layer.schema.fields):
                            raise IntegrityError('record member row does not match its closed logical schema')
                        key = physical['record_identity']
                        if key != record_key(record[layer.schema.identity_field], layer.schema.identity_field) or physical['partition_value'] != record_key(record[layer.schema.partition_field], layer.schema.partition_field):
                            raise IntegrityError('record member routing columns differ from its logical row')
                        if previous is not None and key <= previous:
                            raise IntegrityError('logical record identities are not globally unique and ordered')
                        previous = key
                        if physical['bucket'] != partition_bucket(physical['partition_value'], layer.partition_policy.bucket_count):
                            raise IntegrityError('logical record appears in the wrong partition')
                        yield record

    def stream(self, reference, *, partitions=None):
        """Stream a layer's logical records in identity order."""

        yield from self._rows(self.admitted(reference), partitions=partitions)

    def scan_partition_value(self, reference, partition_value):
        """Stream the records of one partition value."""

        record_key(partition_value, 'partition_value')
        yield from self._rows(self.admitted(reference), partition_value=partition_value)

    def lookup(self, reference, record_id, *, partition_value=None):
        """Return one record by identity, or None when it is absent."""

        record_key(record_id, 'record_id')
        rows = list(self._rows(self.admitted(reference), record_id=record_id, partition_value=partition_value))
        return rows[0] if rows else None

    def lookup_batches(self, reference, record_ids):
        """Stream bounded batches of records for the requested identities."""

        layer = reference if isinstance(reference, AdmittedRecordLayer) else self.admitted(reference)
        with owned_iterator(bounded_rows(record_ids, size=lambda key: len(record_key(key, 'record_id').encode()))) as chunks:
            for keys in chunks:
                with self._relation(layer, record_ids=list(keys)) as relation, closing(relation.order('record_identity').to_arrow_reader(256)) as reader:
                    yield from bounded_batches(reader, byte_column=tuple(_physical_schema(layer.schema).names) if layer.schema.columns else 'record_json', max_value_bytes=self.max_record_bytes)

    def data_files(self, reference):
        """Locators of the data files in a layer's pinned snapshot; layers built on one another share them."""

        for path in snapshot_data_files(self.admitted(reference).table):
            yield path.relative_to(self.root).as_posix()

    @contextmanager
    def file_relation(self, locators):
        """Read whole data files by locator, including rows a sharing snapshot's delete files exclude."""

        paths = [str(_contained(self.root, locator)) for locator in locators]
        with self._cursor() as cursor:
            yield cursor.sql("SELECT record_identity, partition_value, record_json FROM read_parquet(["
                             + ", ".join(literal(path) for path in paths) + "])")

    def physical_references(self, reference):
        """Stream the layer's recovery files followed by its root reference."""

        root = self._verified_root(reference)[0]
        yield from recovery_references(self.root, BlobRef.from_dict(root['integrity']))
        yield BlobRef(reference.state_ref, reference.digest, _contained(self.root, reference.state_ref).stat().st_size, 'application/json')

    def delete(self, reference):
        """Delete the layer's owned files, refusing paths outside its storage directories."""

        if not reference.locator.startswith(('iceberg/', 'record-layers/sha256/')):
            raise IntegrityError('record deletion is outside the owned storage directories')
        return delete_content(self.root, reference)

    def _client(self):
        if self.catalog is None:
            raise IntegrityError('Iceberg writes require DOCSPEC_ICEBERG_URI or an explicit IcebergCatalog')
        if self._catalog_client is None:
            with self._connection_lock:
                if self._catalog_client is None:
                    self._catalog_client = self.catalog.client()
                    self.catalog.attach(self._connection)
        return self._catalog_client

    @contextmanager
    def _write_table(self, cursor, schema, base=None, target_member_bytes=None):
        if base is not None and Path(base.table.metadata.location) != Path(base.table.metadata_location).parent.parent:
            raise IntegrityError('relocated snapshots support reads; writes require the original table directory')
        client = self._client()
        name = 'write_' + uuid4().hex
        key = (self.catalog.namespace, name)
        qualified = f'iceberg.{identifier(key[0])}.{identifier(name)}'
        registered = False
        try:
            if base is None:
                location = self.root / 'iceberg' / uuid4().hex
                (location / 'metadata').mkdir(parents=True)
                (location / 'data').mkdir()
                columns = ', '.join(f'{identifier(field.name)} {"VARCHAR" if pa.types.is_string(field.type) else "BLOB"}' for field in _physical_schema(schema))
                target = target_member_bytes or self.max_member_bytes // 2
                cursor.execute(f"CREATE TABLE {qualified} ({columns}, bucket INTEGER) WITH ("
                    f"'location'={literal(location)}, 'format-version'='2', 'write.update.mode'='merge-on-read', "
                    f"'write.delete.mode'='merge-on-read', 'write.target-file-size-bytes'={literal(target)}, "
                    "'write.parquet.row-group-size-bytes'='1048576')")
            else:
                if base._storage is not self:
                    raise IntegrityError('incremental base belongs to another record store')
                client.register_table(key, str(_contained(self.root, base._root['metadata']['locator'])))
            registered = True
            yield qualified, lambda: client.load_table(key)
        finally:
            # No purge: physical deletion belongs exclusively to CoreMaintenance.
            # A failed drop can leave an unreachable catalog handle, never a
            # published state or permission to collect shared files.
            if registered:
                client.drop_table(key)

    def _pin(self, table, *, schema, partition_policy, layer_kind, record_count):
        location = Path(table.metadata_location)
        from docspec.adapters.storage.iceberg import SnapshotIO
        table.io = SnapshotIO(location.parent.parent, table.metadata.location)
        current = table.current_snapshot()
        if current is not None:
            for manifest in current.manifests(table.io):
                if manifest.added_snapshot_id == current.snapshot_id:
                    for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                        if entry.data_file.file_size_in_bytes > self.max_member_bytes:
                            raise LimitExceededError('record member exceeds its byte limit')
        integrity = seal_snapshot(self.root, table)
        digest, size = sha256_file(location)
        metadata = BlobRef(location.relative_to(self.root).as_posix(), digest, size, 'application/octet-stream')
        root = {'format': 'docspec-iceberg-records', 'version': 1, 'layerKind': layer_kind,
                'schema': self._schema_dict(schema), 'partitionPolicy': self._policy_dict(partition_policy),
                'metadata': metadata.to_dict(), 'integrity': integrity.to_dict(), 'recordCount': record_count}
        payload = canonical_json_file_bytes(root)
        if len(payload) > self.max_root_bytes:
            raise LimitExceededError('record layer root exceeds its byte limit')
        digest = sha256_digest(payload)
        locator = f'record-layers/sha256/{digest[7:9]}/{digest[7:]}.json'
        _write_once(self.root, locator, payload)
        ref = LayerRef(stable_urn('record-layer', root), layer_kind, schema.schema_id, _PROFILE_ID, locator, digest, record_count)
        return self._remember_admitted(AdmittedRecordLayer(self, ref, root, schema, partition_policy, table))

    @contextmanager
    def _incoming(self, cursor, batches, schema, policy, *, ordered=True, allow_deletes=False):
        physical = _physical_schema(schema)
        output = pa.schema([*physical, ('bucket', pa.int32())])
        error = None
        def checked():
            nonlocal error
            previous = None
            try:
                with owned_iterator(batches) as source:
                    for batch in source:
                        if not batch.schema.equals(physical, check_metadata=False):
                            raise IntegrityError('record batch has an invalid physical schema')
                        if any(batch.column(name).null_count for name in ('record_identity', 'partition_value')) or (not allow_deletes and batch.column('record_json').null_count):
                            raise IntegrityError('record batch columns must not contain nulls')
                        with closing(bounded_batches([batch], byte_column=tuple(physical.names) if schema.columns else 'record_json', max_value_bytes=self.max_record_bytes, allow_null=allow_deletes)) as parts:
                            for part in parts:
                                keys = part.column('record_identity').to_pylist()
                                for key in keys:
                                    if ordered and previous is not None and key <= previous:
                                        raise IntegrityError('record input must be strictly ordered by logical identity')
                                    previous = key
                                values = part.column('partition_value').to_pylist()
                                buckets = [partition_bucket(value, policy.bucket_count) for value in values]
                                yield pa.RecordBatch.from_arrays([*part.columns, pa.array(buckets, type=pa.int32())], schema=output)
            except BaseException as exc:
                if not isinstance(exc, GeneratorExit):
                    error = exc
                raise
        with closing(checked()) as source, closing(pa.RecordBatchReader.from_batches(output, source)) as reader:
            cursor.register('incoming_stream', reader.__arrow_c_stream__())
            try:
                cursor.execute('CREATE TEMP TABLE incoming AS SELECT * FROM incoming_stream')
                if cursor.execute('SELECT 1 FROM incoming GROUP BY record_identity HAVING count(*)>1 LIMIT 1').fetchone():
                    raise IntegrityError('record input contains duplicate logical identities')
                yield cursor.table('incoming')
            except BaseException:
                if error is not None:
                    raise error
                raise
            finally:
                cursor.unregister('incoming_stream')
                cursor.execute('DROP TABLE IF EXISTS incoming')

    def _write_batches(self, batches, *, layer_kind, schema, partition_policy, base=None, replace_partitions=None,
                       ordered=True, target_member_bytes=None):
        require_text(layer_kind, 'layer_kind')
        if (base is None) != (replace_partitions is None):
            raise ValueError('incremental layers require a base and replacement partitions together')
        if isinstance(base, LayerRef):
            base = self.admitted(base)
        if base is not None and (base.schema != schema or base.partition_policy != partition_policy or base.reference.layer_kind != layer_kind):
            raise IntegrityError('incremental layer is incompatible with its base')
        if target_member_bytes is not None and not 0 < target_member_bytes <= self.max_member_bytes:
            raise ValueError('target member bytes must fit the member byte limit')
        if replace_partitions is not None and any(p < 0 or p >= partition_policy.bucket_count for p in replace_partitions):
            raise ValueError('replacement partition is outside the layer partition policy')
        with self._cursor() as cursor, self._incoming(cursor, batches, schema, partition_policy, ordered=ordered):
            count = cursor.execute('SELECT count(*) FROM incoming').fetchone()[0]
            if not count and base is not None and not replace_partitions:
                return base
            with self._write_table(cursor, schema, base, target_member_bytes) as (target, table):
                cursor.execute('BEGIN')
                try:
                    if base is not None:
                        selected = ', '.join(str(p) for p in replace_partitions)
                        condition = f'bucket IN ({selected})' if selected else 'false'
                        if cursor.execute(f'SELECT 1 FROM incoming WHERE NOT ({condition}) LIMIT 1').fetchone():
                            raise IntegrityError('incremental records include a partition not declared for replacement')
                        if cursor.execute(f'SELECT 1 FROM incoming JOIN (SELECT record_identity FROM {target} WHERE NOT ({condition})) kept USING (record_identity) LIMIT 1').fetchone():
                            raise IntegrityError('replacement duplicates an identity outside its selected partitions')
                        removed = cursor.execute(f'SELECT count(*) FROM {target} WHERE {condition}').fetchone()[0]
                        cursor.execute(f'DELETE FROM {target} WHERE {condition}')
                        count += base.reference.record_count - removed
                    cursor.execute(f'INSERT INTO {target} SELECT * FROM incoming ORDER BY record_identity')
                    cursor.execute('COMMIT')
                except BaseException:
                    cursor.execute('ROLLBACK')
                    raise
                return self._pin(table(), schema=schema, partition_policy=partition_policy, layer_kind=layer_kind, record_count=count)

    def apply_changes(self, base, batches):
        """Upsert only changed keys; a null record_json means remove that key."""
        with self._cursor() as cursor, self._incoming(cursor, batches, base.schema, base.partition_policy,
                                                    ordered=False, allow_deletes=True):
            if not cursor.execute('SELECT 1 FROM incoming LIMIT 1').fetchone():
                return base
            with self._write_table(cursor, base.schema, base) as (target, table):
                columns = [*_physical_schema(base.schema).names, 'bucket']
                matches = 't.record_identity=s.record_identity'
                inserted, removed = cursor.execute(f'SELECT count(*) FILTER (WHERE t.record_identity IS NULL AND s.record_json IS NOT NULL), '
                    f'count(*) FILTER (WHERE t.record_identity IS NOT NULL AND s.record_json IS NULL) FROM incoming s LEFT JOIN {target} t ON {matches}').fetchone()
                updates = ', '.join(f'{identifier(name)}=s.{identifier(name)}' for name in columns[1:])
                names = ', '.join(identifier(name) for name in columns)
                values = ', '.join('s.' + identifier(name) for name in columns)
                cursor.execute('BEGIN')
                try:
                    # This extension supports one matched action per MERGE.
                    # Both statements commit as one Iceberg transaction.
                    if removed:
                        cursor.execute(f'MERGE INTO {target} t USING (SELECT * FROM incoming WHERE record_json IS NULL) s ON {matches} WHEN MATCHED THEN DELETE')
                    cursor.execute(f'MERGE INTO {target} t USING (SELECT * FROM incoming WHERE record_json IS NOT NULL) s ON {matches} '
                        f'WHEN MATCHED THEN UPDATE SET {updates} '
                        f'WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({values})')
                    cursor.execute('COMMIT')
                except BaseException:
                    cursor.execute('ROLLBACK')
                    raise
                return self._pin(table(), schema=base.schema, partition_policy=base.partition_policy,
                                 layer_kind=base.reference.layer_kind, record_count=base.reference.record_count + inserted - removed)

    def union_disjoint(self, base, changes, *, exclude_existing=False):
        """Union compatible admitted layers, refusing overlapping identities unless they are excluded."""

        if base._storage is not self or changes._storage is not self or base.schema != changes.schema or base.partition_policy != changes.partition_policy or base.reference.layer_kind != changes.reference.layer_kind:
            raise IntegrityError('record union requires compatible admitted layers')
        if not changes.reference.record_count:
            return base
        with self.relations({'base': base, 'changes': changes}) as relations:
            existing = relations['base'].project('record_identity AS existing_key')
            incoming = relations['changes']
            if not exclude_existing and incoming.join(existing, 'record_identity=existing_key', how='semi').limit(1).fetchone():
                raise IntegrityError('record union requires disjoint logical identities')
            added = incoming.join(existing, 'record_identity=existing_key', how='anti').order('record_identity')
            with closing(added.to_arrow_reader(256)) as batches:
                return self.apply_changes(base, batches)


@dataclass(frozen=True, slots=True)
class AdmittedRecordLayer:
    """An admitted immutable layer carrying its reference, schema, policy and static table."""

    _storage: IcebergRecordStorage
    reference: LayerRef
    _root: Mapping[str, Any]
    schema: RecordSchema
    partition_policy: PartitionPolicy
    table: Any

    @contextmanager
    def relation(self, *, partitions=None):
        """Open this layer's native relation, optionally restricted to partitions."""

        with self._storage._relation(self, partitions=partitions) as relation:
            yield relation

    def batches(self, *, partitions=None):
        """Stream byte-bounded record batches in identity order."""

        with self.relation(partitions=partitions) as relation, closing(relation.order('record_identity').to_arrow_reader(256)) as reader:
            yield from bounded_batches(reader, byte_column=tuple(_physical_schema(self.schema).names) if self.schema.columns else 'record_json', max_value_bytes=self._storage.max_record_bytes)
