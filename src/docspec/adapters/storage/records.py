"""Immutable Iceberg snapshots of encoded records or typed tables, with DuckDB owning bulk data writes.

Both root formats share one connection, admission cache, catalog write
handle, pin and seal, so both profiles live in this one store; each refuses
the other's layers.
"""

from __future__ import annotations

import os
import shutil
import sys
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
import pyarrow.parquet as pq
from pyiceberg.io.pyarrow import UnsupportedPyArrowTypeException

from docspec.ports.record_storage import bounded_batches, bounded_rows
from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, conform_table_batch, encoded_batches, table_arrow_schema
from docspec.adapters.streams import owned_iterator
from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES, connect
from docspec.adapters.storage.files import _available_paths, _contained, _read_exact, _storage_root, _write_once, delete_content, sha256_file
from docspec.adapters.storage.iceberg import (IcebergCatalog, SnapshotIO, recovery_references, identifier, literal, seal,
    seal_snapshot, snapshot, snapshot_data_files, snapshot_files, table_columns)
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, parse_canonical_json, require_sha256, require_text, sha256_digest, stable_urn, thaw_json
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, TableSchema, partition_bucket, record_key
from docspec.errors import IntegrityError, LimitExceededError

_PROFILE_ID = 'urn:docspec:profile:record-storage:iceberg:1'
_TABLE_PROFILE_ID = 'urn:docspec:profile:table-storage:iceberg:1'
_RECORD_ROOT = {'format', 'version', 'layerKind', 'schema', 'partitionPolicy', 'metadata', 'integrity', 'recordCount'}
_TABLE_ROOT = {'format', 'version', 'layerKind', 'schema', 'memberDigest', 'metadata', 'integrity', 'recordCount'}
_ADMITTED_LAYER_LIMIT = 8
# DuckDB prunes row groups for each value of an IN list, so point lookups stay
# bounded. A semi-join holding more than 50 values prunes only by their overall
# range; past this many values, one scan is the cheaper plan.
LITERAL_IDENTITIES = 256


def identity_filter(cursor, relation, values, column='record_identity'):
    """Keep only rows whose ``column`` is one of ``values``: strings or bytes, NUL included.

    Up to ``LITERAL_IDENTITIES`` values are one IN filter of constants; more
    are one semi-join against an Arrow table, never a query per chunk.
    """
    values = list(values)
    if not values:
        return relation.filter('false')
    if len(values) <= LITERAL_IDENTITIES:
        return relation.filter(duckdb.ColumnExpression(column).isin(*map(duckdb.ConstantExpression, values)))
    wanted = cursor.from_arrow(pa.table({'wanted_identity': pa.array(values)}))
    return relation.join(wanted, f'{identifier(column)} = wanted_identity', how='semi')


@dataclass(frozen=True, slots=True)
class LayerFiles:
    """What a member search keeps of an admitted layer: its reference, commit time and data files."""

    reference: LayerRef
    committed_ms: int
    files: tuple[str, ...]
    deletes: bool


def _physical_schema(schema):
    return pa.schema([*ENCODED_RECORD_SCHEMA, *(pa.field(name, pa.string() if kind == 'string' else pa.binary())
                                         for name, kind in schema.columns)])


def _column_list(schema):
    return ', '.join(identifier(name) for name in _physical_schema(schema).names)


_ENCODED_ONLY = 'layer is a typed table; the encoded-record profile refuses it'


def _encoded(layer):
    if not isinstance(layer, AdmittedRecordLayer):
        raise IntegrityError(_ENCODED_ONLY)
    return layer


def _typed(layer):
    if not isinstance(layer, AdmittedTableLayer):
        raise IntegrityError('layer holds encoded records; the table profile refuses it')
    return layer


def native_columns(relation):
    """Name each column of a native relation with its table-profile type, or DuckDB's name for any other."""
    return tuple((name, 'TIMESTAMPTZ' if str(kind) == 'TIMESTAMP WITH TIME ZONE' else str(kind))
                 for name, kind in zip(relation.columns, relation.types, strict=True))


def _has_field_id(field):
    """Whether an Arrow field read from a Parquet footer, or a field nested in it, carries an ID."""
    return (b'PARQUET:field_id' in (field.metadata or {})
            or any(_has_field_id(field.type.field(index)) for index in range(field.type.num_fields)))


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
        # Producer files are staged here, on the store's own filesystem, for registration.
        self.staging_directory = _contained(self.root, '.staging/member', create_parents=create).parent
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

    def admitted(self, reference: LayerRef) -> AdmittedRecordLayer | AdmittedTableLayer:
        """Use this protection scope's handle, or freshly check availability."""
        layers = getattr(self._admissions, "layers", None)
        if layers is not None and reference in layers:
            return self._remember_admitted(layers[reference])
        return self.available(reference)

    @staticmethod
    def _schema_dict(schema: RecordSchema | TableSchema) -> dict[str, Any]:
        if isinstance(schema, TableSchema):
            return {"schemaId": schema.schema_id, "columns": [list(column) for column in schema.columns]}
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

        return self._record_root(reference)[0].identity_field

    def compact(self, base: AdmittedRecordLayer) -> AdmittedRecordLayer:
        """Repack admitted canonical bytes and check exact row equivalence."""
        if _encoded(base)._storage is not self:
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

    def schema(self, reference: LayerRef) -> RecordSchema | TableSchema:
        """Return the verified layer's logical schema, a TableSchema for a typed table."""

        return self._verified_root(reference)[1]

    def _record_root(self, reference):
        _, schema, policy = self._verified_root(reference)
        if isinstance(schema, TableSchema):
            raise IntegrityError(_ENCODED_ONLY)
        return schema, policy

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy:
        """Return the verified encoded layer's partition policy; typed tables have none."""

        return self._record_root(reference)[1]

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
        typed = isinstance(root, dict) and root.get('format') == 'docspec-iceberg-table'
        if (not isinstance(root, dict) or set(root) != (_TABLE_ROOT if typed else _RECORD_ROOT)
                or root['format'] not in {'docspec-iceberg-records', 'docspec-iceberg-table'}
                or type(root['version']) is not int or root['version'] != 1 or type(root['recordCount']) is not int):
            raise IntegrityError('record layer root has an invalid format')
        try:
            value = root['schema']
            if typed:
                if set(value) != {'schemaId', 'columns'}:
                    raise ValueError('invalid table schema shape')
                schema, policy = TableSchema(value['schemaId'], tuple(tuple(column) for column in value['columns'])), None
                if root['memberDigest'] is not None:
                    require_sha256(root['memberDigest'], 'table member digest')
            else:
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
        expected = LayerRef(stable_urn('record-layer', root), root['layerKind'], schema.schema_id, _TABLE_PROFILE_ID if typed else _PROFILE_ID,
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
        if isinstance(schema, TableSchema):
            if table_columns(table.schema()) != schema.columns:
                raise IntegrityError('table layer schema differs from its pinned Iceberg metadata')
            return self._remember_admitted(AdmittedTableLayer(self, reference, root, schema, table))
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
        """Verify member files and the declared record count.

        A typed table also compares a native count(*) and the scan's column
        types with its description, and a registered table checks that its one
        data file is the producer member its root names.
        """

        self.verify_members(reference)
        layer = self.available(reference)
        if isinstance(layer, AdmittedRecordLayer):
            if sum(1 for _ in self.stream(reference)) != reference.record_count:
                raise IntegrityError('record count differs from its description')
            return
        with self._relation(layer) as relation:
            if native_columns(relation) != layer.schema.columns:
                raise IntegrityError('table scan columns differ from the layer schema')
            if relation.aggregate('count(*)').fetchone()[0] != reference.record_count:
                raise IntegrityError('table row count differs from its description')
        if layer.member_digest is not None:
            digests = {ref.locator: ref.digest for ref in self.physical_references(reference)}
            files = [digests.get(path.relative_to(self.root).as_posix()) for path, _ in snapshot_data_files(layer.table)]
            if files != [layer.member_digest]:
                raise IntegrityError('registered table differs from its producer member')

    def admit(self, reference):
        """Fully verify a layer and return it admitted."""

        self.verify(reference)
        return self.available(reference)

    @contextmanager
    def _relation(self, layer, *, cursor=None, partitions=None, record_ids=None, identity_ranges=None, include_bucket=False):
        if layer._storage is not self:
            raise IntegrityError('admitted layer belongs to another record store')
        if partitions is not None or record_ids is not None or identity_ranges is not None or include_bucket:
            _encoded(layer)
        if partitions is not None and any(p < 0 or p >= layer.partition_policy.bucket_count for p in partitions):
            raise ValueError('selected partition is outside the layer partition policy')
        path = Path(layer.table.metadata_location)
        with (self._cursor() if cursor is None else nullcontext(cursor)) as cursor:
            # Explicit version plus moved-path resolution reads exactly the pin,
            # never a catalog head, directory glob or version-hint file.
            relation = cursor.sql(f"SELECT * FROM iceberg_scan({literal(path.parent.parent)}, "
                f"version={literal(path.name.removesuffix('.metadata.json'))}, "
                "version_name_format='%s%s.metadata.json', allow_moved_paths=true)")
            if isinstance(layer, AdmittedTableLayer):
                yield relation.project(', '.join(identifier(name) for name in layer.schema.fields))
                return
            if partitions is not None:
                relation = relation.filter(duckdb.ColumnExpression('bucket').isin(*(duckdb.ConstantExpression(p) for p in partitions))) if partitions else relation.filter('false')
            if record_ids is not None:
                relation = identity_filter(cursor, relation, record_ids)
            if identity_ranges is not None:
                ranges = list(identity_ranges)
                relation = relation.filter(' OR '.join(f'(record_identity BETWEEN {literal(lo)} AND {literal(hi)})' for lo, hi in ranges) or 'false')
            yield relation.project(_column_list(layer.schema) + (', bucket' if include_bucket else ''))

    @contextmanager
    def relations(self, references, *, partitions=None, tables=None, identities=None, identity_ranges=None, cursor=None):
        """Open named native relations over admitted layers and caller-supplied tables.

        A typed table's relation holds its own columns in schema order; the
        encoded-record routing options (partitions, identities, ranges) refuse it.
        """

        with (self._cursor() if cursor is None else nullcontext(cursor)) as cursor, ExitStack() as stack:
            result = {}
            for name, reference in references.items():
                layer = self.admitted(reference) if isinstance(reference, LayerRef) else reference
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
        with self._relation(_encoded(layer), partitions=partitions, record_ids=None if record_id is None else [record_id], include_bucket=True) as relation:
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

        layer = _encoded(self.admitted(reference) if isinstance(reference, LayerRef) else reference)
        with owned_iterator(bounded_rows(record_ids, size=lambda key: len(record_key(key, 'record_id').encode()))) as chunks:
            for keys in chunks:
                with self._relation(layer, record_ids=list(keys)) as relation, closing(relation.order('record_identity').to_arrow_reader(256)) as reader:
                    yield from bounded_batches(reader, byte_column=tuple(_physical_schema(layer.schema).names) if layer.schema.columns else 'record_json', max_value_bytes=self.max_record_bytes)

    def data_files(self, reference):
        """Locators of the data files in a layer's pinned snapshot; layers built on one another share them."""

        return self.layer_files(reference).files

    def layer_files(self, reference):
        """Admit a layer and keep only its commit time and data file locators, not its Iceberg metadata."""

        table = self.admitted(reference).table
        files, deletes = [], False
        for path, delete in snapshot_data_files(table):
            if delete:
                deletes = True
            else:
                # Layers built on one another list the same files; share the names.
                files.append(sys.intern(path.relative_to(self.root).as_posix()))
        return LayerFiles(reference, table.metadata.last_updated_ms, tuple(files), deletes)

    @contextmanager
    def file_relation(self, locators, *, identities=None):
        """Read whole data files by locator, including rows a sharing snapshot's delete files exclude.

        The relation names each row's file by locator; ``identities`` keeps
        only those records.
        """

        paths = {str(_contained(self.root, locator)): locator for locator in locators}
        with self._cursor() as cursor:
            relation = cursor.sql("SELECT record_identity, partition_value, record_json, filename FROM read_parquet(["
                                  + ", ".join(literal(path) for path in paths) + "], filename=true)")
            if identities is not None:
                relation = identity_filter(cursor, relation, identities)
            mapping = cursor.from_arrow(pa.table({'file_path': list(paths), 'locator': list(paths.values())}))
            yield relation.join(mapping, 'filename = file_path').project('record_identity, partition_value, record_json, locator')

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
                if isinstance(schema, TableSchema):
                    columns = ', '.join(f'{identifier(name)} {kind}' for name, kind in schema.columns)
                else:
                    columns = ', '.join(f'{identifier(field.name)} {"VARCHAR" if pa.types.is_string(field.type) else "BLOB"}'
                                        for field in _physical_schema(schema)) + ', bucket INTEGER'
                target = target_member_bytes or self.max_member_bytes // 2
                cursor.execute(f"CREATE TABLE {qualified} ({columns}) WITH ("
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

    def _pin(self, table, *, schema, layer_kind, record_count, partition_policy=None, member_digest=None):
        location = Path(table.metadata_location)
        table.io = SnapshotIO(location.parent.parent, table.metadata.location)
        current = table.current_snapshot()
        # The member limit sizes DocSpec's own writes. A registered producer
        # file is exempt; registration bounded its row groups instead.
        if current is not None and member_digest is None:
            for manifest in current.manifests(table.io):
                if manifest.added_snapshot_id == current.snapshot_id:
                    for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                        if entry.data_file.file_size_in_bytes > self.max_member_bytes:
                            raise LimitExceededError('record member exceeds its byte limit')
        integrity = seal_snapshot(self.root, table)
        digest, size = sha256_file(location)
        metadata = BlobRef(location.relative_to(self.root).as_posix(), digest, size, 'application/octet-stream')
        typed = isinstance(schema, TableSchema)
        root = {'format': 'docspec-iceberg-table' if typed else 'docspec-iceberg-records', 'version': 1,
                'layerKind': layer_kind, 'schema': self._schema_dict(schema),
                'metadata': metadata.to_dict(), 'integrity': integrity.to_dict(), 'recordCount': record_count}
        if typed:
            root['memberDigest'] = member_digest
        else:
            root['partitionPolicy'] = self._policy_dict(partition_policy)
        payload = canonical_json_file_bytes(root)
        if len(payload) > self.max_root_bytes:
            raise LimitExceededError('record layer root exceeds its byte limit')
        digest = sha256_digest(payload)
        locator = f'record-layers/sha256/{digest[7:9]}/{digest[7:]}.json'
        _write_once(self.root, locator, payload)
        ref = LayerRef(stable_urn('record-layer', root), layer_kind, schema.schema_id,
                       _TABLE_PROFILE_ID if typed else _PROFILE_ID, locator, digest, record_count)
        return self._remember_admitted(AdmittedTableLayer(self, ref, root, schema, table) if typed
                                       else AdmittedRecordLayer(self, ref, root, schema, partition_policy, table))

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
        if not isinstance(schema, RecordSchema):
            raise IntegrityError('typed tables are written with write_table, not the encoded-record writer')
        if (base is None) != (replace_partitions is None):
            raise ValueError('incremental layers require a base and replacement partitions together')
        if isinstance(base, LayerRef):
            base = self.admitted(base)
        if base is not None and (_encoded(base).schema != schema or base.partition_policy != partition_policy or base.reference.layer_kind != layer_kind):
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
        with self._cursor() as cursor, self._incoming(cursor, batches, _encoded(base).schema, base.partition_policy,
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

        if _encoded(base)._storage is not self or _encoded(changes)._storage is not self or base.schema != changes.schema or base.partition_policy != changes.partition_policy or base.reference.layer_kind != changes.reference.layer_kind:
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

    def write_table(self, batches: Iterable[pa.RecordBatch], *, layer_kind: str, schema: TableSchema,
                    sort_by: tuple[str, ...] = ()) -> AdmittedTableLayer:
        """Write typed Arrow batches natively into a new table layer.

        Batches match ``schema`` as ``table_arrow_schema`` spells it; only a
        TIMESTAMPTZ zone name may differ, so a nanosecond timestamp refuses.
        DuckDB writes Parquet carrying Iceberg field IDs, in the store's bounded
        row groups and member-sized files. ``sort_by`` orders the whole write,
        so each new file is sorted and its row groups prune on those columns.
        """
        require_text(layer_kind, 'layer_kind')
        if not isinstance(schema, TableSchema):
            raise IntegrityError('encoded records are written with write_batches, not the table writer')
        return self._write_rows(batches, schema=schema, layer_kind=layer_kind, sort_by=sort_by)

    def append_table(self, base, batches: Iterable[pa.RecordBatch], *, sort_by: tuple[str, ...] = ()) -> AdmittedTableLayer:
        """Add typed rows to a natively written table as new files, sharing every base file.

        Rows are only added, never replaced or removed. A registered producer
        table is a sealed generation and refuses. No rows returns ``base``.
        """
        base = _typed(self.admitted(base) if isinstance(base, LayerRef) else base)
        if base.member_digest is not None:
            raise IntegrityError('a registered producer table is sealed and refuses appended rows')
        return self._write_rows(batches, schema=base.schema, layer_kind=base.reference.layer_kind, sort_by=sort_by, base=base)

    def _write_rows(self, batches, *, schema, layer_kind, sort_by, base=None):
        if len(set(sort_by)) != len(sort_by) or not set(sort_by) <= set(schema.fields):
            raise ValueError('sort columns must be distinct table columns')
        expected = table_arrow_schema(schema.columns)
        count, error = 0, None
        def checked():
            nonlocal count, error
            try:
                with owned_iterator(batches) as source:
                    for batch in source:
                        batch = conform_table_batch(batch, expected)
                        count += batch.num_rows
                        yield batch
            except BaseException as exc:
                if not isinstance(exc, GeneratorExit):
                    error = exc
                raise
        order = ' ORDER BY ' + ', '.join(identifier(name) for name in sort_by) if sort_by else ''
        with self._cursor() as cursor, closing(checked()) as source, \
                closing(pa.RecordBatchReader.from_batches(expected, source)) as reader:
            cursor.register('table_rows', reader.__arrow_c_stream__())
            try:
                with self._write_table(cursor, schema, base) as (target, table):
                    cursor.execute(f'INSERT INTO {target} SELECT * FROM table_rows{order}')
                    if base is not None and not count:
                        return base
                    return self._pin(table(), schema=schema, layer_kind=layer_kind,
                                     record_count=count + (0 if base is None else base.reference.record_count))
            except BaseException:
                if error is not None:
                    raise error
                raise
            finally:
                cursor.unregister('table_rows')

    def register_parquet(self, path: Path, *, layer_kind: str, schema: TableSchema, member_digest: str) -> AdmittedTableLayer:
        """Register a producer's Parquet file, staged in ``staging_directory``, as a table layer without rewriting it.

        The footer is checked first, and nothing is placed if it refuses: its
        columns must read as ``schema`` declares them, and a file already
        carrying field IDs refuses, since Iceberg ``add_files`` reads through a
        name mapping. The file is exempt from ``max_member_bytes``, which sizes
        DocSpec's own writes; a row group whose uncompressed data exceeds it
        refuses instead. The file is then hard-linked, never copied or
        replaced, to ``iceberg/member-<digest>/data/member.parquet``, and its
        seal must equal ``member_digest``. A refusal removes only what this call
        placed and keeps the stage; success unlinks the stage. An interrupted
        registration leaves that directory where cleanup can name it, and a
        retry reuses a placed file holding the member's bytes. Register one
        member at a time.
        """
        require_text(layer_kind, 'layer_kind')
        require_sha256(member_digest, 'member digest')
        if not isinstance(schema, TableSchema):
            raise IntegrityError('a registered producer file needs a declared table schema')
        source = Path(path)
        try:
            staged = source.parent.resolve(strict=True).is_relative_to(self.staging_directory.resolve(strict=True))
        except OSError:
            staged = False
        if not staged or source.is_symlink() or not source.is_file():
            raise IntegrityError("a registered producer file must be a regular file in the store's staging directory")
        with pq.ParquetFile(source) as parquet:
            footer = parquet.metadata
        arrow_schema = footer.schema.to_arrow_schema()
        if any(_has_field_id(field) for field in arrow_schema):
            raise IntegrityError('registered Parquet already carries field IDs')
        if any(footer.row_group(index).total_byte_size > self.max_member_bytes for index in range(footer.num_row_groups)):
            raise LimitExceededError('registered Parquet row group exceeds the member byte limit')
        with self._cursor() as cursor:
            if native_columns(cursor.read_parquet(str(source))) != schema.columns:
                raise IntegrityError('registered Parquet footer differs from its declared schema')
        directory = f'iceberg/member-{member_digest[7:]}'
        created, fresh = not (self.root / directory).exists(), False
        placed = self.root / directory / 'data' / 'member.parquet'
        receipt = placed.with_name(placed.name + '.sha256')
        try:
            _contained(self.root, f'{directory}/metadata/placeholder', create_parents=True)
            _contained(self.root, f'{directory}/data/member.parquet', create_parents=True)
            if not placed.exists():
                receipt.unlink(missing_ok=True)  # it describes no file, so seal must hash the new one
            try:
                os.link(source, placed)
                fresh = True
            except FileExistsError:
                pass  # an interrupted registration placed it; its bytes are checked below
            sealed = next(recovery_references(self.root, seal(self.root, placed))).digest
            # An interrupted attempt's receipt says nothing of the bytes placed now.
            if sealed != member_digest or (not fresh and sha256_file(placed)[0] != member_digest):
                raise IntegrityError('registered Parquet differs from its member digest')
            with self._cursor():  # the catalog attaches to the native connection a cursor opens
                client = self._client()
            key, handle = (self.catalog.namespace, 'register_' + uuid4().hex), None
            try:
                handle = client.create_table(key, schema=arrow_schema, location=str(self.root / directory))
                handle.add_files([str(placed)])
                table = client.load_table(key)
            except (NotImplementedError, TypeError, ValueError, UnsupportedPyArrowTypeException) as error:
                raise IntegrityError(f'Iceberg refused the registered Parquet: {error}') from error
            finally:
                # No purge, as for every write handle: the files stay ours.
                if handle is not None:
                    client.drop_table(key)
            if table_columns(table.schema()) != schema.columns:
                raise IntegrityError('registered Iceberg schema differs from its declared schema')
            layer = self._pin(table, schema=schema, layer_kind=layer_kind, record_count=footer.num_rows,
                              member_digest=member_digest)
        except BaseException:
            if created:
                shutil.rmtree(self.root / directory, ignore_errors=True)
            elif fresh:
                placed.unlink(missing_ok=True)
                receipt.unlink(missing_ok=True)
            raise
        source.unlink()
        return layer


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


@dataclass(frozen=True, slots=True)
class AdmittedTableLayer:
    """An admitted typed table: its reference, closed schema, pinned table and any producer member digest."""

    _storage: IcebergRecordStorage
    reference: LayerRef
    _root: Mapping[str, Any]
    schema: TableSchema
    table: Any

    @property
    def member_digest(self) -> str | None:
        """The producer member a registered table holds unchanged; None for DocSpec's own writes."""
        return self._root['memberDigest']

    @contextmanager
    def relation(self):
        """Open this table's native relation: its own columns, in schema order."""

        with self._storage._relation(self) as relation:
            yield relation
