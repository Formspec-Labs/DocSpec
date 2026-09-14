"""Immutable Parquet record layers, queried and sorted by DuckDB."""

from __future__ import annotations

import tempfile
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, local
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from docspec.ports.record_storage import BATCH_ROWS, bounded_batches, bounded_rows
from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.streams import owned_iterator
from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES, connect
from docspec.adapters.storage.files import (
    _available_paths,
    _contained,
    _link_content,
    _read_exact,
    _storage_root,
    _sync_file,
    _verified_member_path,
    _write_once,
    delete_content,
    sha256_file,
)
from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    parse_canonical_json,
    require_relative_path,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket, record_key
from docspec.errors import IntegrityError, LimitExceededError

_PROFILE_ID = "urn:docspec:profile:record-storage:local-parquet:3"
_MEDIA_TYPE = "application/vnd.apache.parquet"
_ADMITTED_LAYER_LIMIT = 8
_PARQUET_SCHEMA = ENCODED_RECORD_SCHEMA
def _physical_schema(schema):
    return pa.schema([*_PARQUET_SCHEMA, *(pa.field(name, pa.string() if kind == "string" else pa.binary())
                                         for name, kind in schema.columns)])


def _column_list(schema):
    return ", ".join('"' + name.replace('"', '""') + '"' for name in _physical_schema(schema).names)


def _overlapping_members(members, ranges):
    """Conservatively select files intersecting any requested identity range."""
    merged = []
    for lower, upper in sorted(ranges):
        if merged and lower <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
        else:
            merged.append((lower, upper))
    starts = [lower for lower, _ in merged]
    selected = []
    for member in members:
        index = bisect_right(starts, member["identityMax"]) - 1
        if index >= 0 and merged[index][1] >= member["identityMin"]:
            selected.append(member)
    return selected


class LocalParquetRecordStorage:
    """Partitioned canonical records with explicit physical admission.

    Queries check consumed logical rows. verify_members freshly admits
    physical files; verify additionally audits every logical row. Readers
    decide their admission lifetime, independently of the native connection.
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
    ) -> None:
        if min(max_member_bytes, max_record_bytes, max_root_bytes, max_merge_scratch_bytes, engine_memory_bytes) <= 0:
            raise ValueError("record storage limits must be positive")
        self.root = _storage_root(root, create=create)
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
        self._staging = _contained(self.root, ".staging/records", create_parents=create).parent
        if create:
            self._staging.mkdir(exist_ok=True)

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
                self._scratch = scratch
            cursor = self._connection.cursor()
        try:
            yield cursor
        except duckdb.OutOfMemoryException as error:
            raise LimitExceededError("record query exceeds its native memory allowance") from error
        except duckdb.Error as error:
            raise IntegrityError(f"record Parquet operation failed: {error}") from error
        finally:
            cursor.close()

    def close(self) -> None:
        """Release native resources after all workers and iterators have stopped."""
        with self._connection_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
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
    def _bucket(value: str, count: int) -> int:
        return partition_bucket(value, count)

    @staticmethod
    def _member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest).removeprefix("sha256:")
        return f"record-members/sha256/{hexadecimal[:2]}/{hexadecimal}.parquet"

    def _load_root(self, reference: LayerRef) -> dict[str, Any]:
        payload = _read_exact(self.root, reference.state_ref, max_bytes=self.max_root_bytes)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("record layer root differs from its reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.layer_id))
        if not isinstance(value, dict):
            raise IntegrityError("record layer root must be a JSON object")
        return value

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

    def _verified_root(self, reference: LayerRef) -> tuple[dict[str, Any], RecordSchema, PartitionPolicy]:
        root = self._load_root(reference)
        if (
            set(root) != {"format", "formatVersion", "layerId", "layerKind", "schema", "profileId", "partitionPolicy", "members", "recordCount"}
            or root["format"] != "docspec-record-layer" or root["formatVersion"] != "4.0"
        ):
            raise IntegrityError("record layer root has an unknown format or invalid closed shape")
        value = root["schema"]
        if not isinstance(value, dict) or set(value) != {"schemaId", "fields", "identityField", "partitionField", "columns"} or not isinstance(value["fields"], list):
            raise IntegrityError("record layer schema has an invalid closed shape")
        if not isinstance(value["columns"], list) or any(not isinstance(column, list) or len(column) != 2 for column in value["columns"]):
            raise IntegrityError("record layer typed columns have an invalid shape")
        schema = RecordSchema(value["schemaId"], tuple(value["fields"]), value["identityField"], value["partitionField"],
                              tuple(tuple(column) for column in value["columns"]))
        value = root["partitionPolicy"]
        if not isinstance(value, dict) or set(value) != {"policyId", "bucketCount"}:
            raise IntegrityError("record layer partition policy has an invalid closed shape")
        policy = PartitionPolicy(value["policyId"], value["bucketCount"])
        if root["profileId"] != _PROFILE_ID:
            raise IntegrityError("record layer names an unknown storage profile")
        content = {key: root[key] for key in ("layerKind", "schema", "profileId", "partitionPolicy", "members", "recordCount")}
        if stable_urn("record-layer", content) != root["layerId"]:
            raise IntegrityError("record layer identity differs from its canonical content")
        if (
            reference.layer_id != root["layerId"] or reference.layer_kind != root["layerKind"]
            or reference.schema_id != schema.schema_id or reference.profile_id != root["profileId"]
            or reference.record_count != root["recordCount"]
        ):
            raise IntegrityError("record layer root differs from its reference")
        if reference.state_ref != f"record-layers/sha256/{reference.digest[7:9]}/{reference.digest[7:]}.json":
            raise IntegrityError("record layer locator differs from its digest")
        if not isinstance(root["members"], list):
            raise IntegrityError("record layer members must be a list")
        previous: tuple[int, int] | None = None
        paths: set[str] = set()
        total = 0
        for member in root["members"]:
            if not isinstance(member, dict) or set(member) != {"partition", "sequence", "path", "mediaType", "byteSize", "digest", "recordCount", "schemaId", "identityMin", "identityMax"}:
                raise IntegrityError("record layer member has an invalid closed shape")
            for name in ("partition", "sequence", "byteSize", "recordCount"):
                if not isinstance(member[name], int) or isinstance(member[name], bool) or member[name] < 0:
                    raise IntegrityError("record member counts and positions must be non-negative integers")
            lower = record_key(member["identityMin"], "record member identityMin")
            upper = record_key(member["identityMax"], "record member identityMax")
            if lower > upper or member["recordCount"] == 0:
                raise IntegrityError("record member identity bounds require a nonempty ordered range")
            if member["partition"] >= policy.bucket_count:
                raise IntegrityError("record member partition is outside its policy")
            key = member["partition"], member["sequence"]
            expected_sequence = previous[1] + 1 if previous is not None and previous[0] == key[0] else 0
            if (previous is not None and key <= previous) or key[1] != expected_sequence:
                raise IntegrityError("record layer partition shards must be distinct, ordered and contiguous")
            previous = key
            require_sha256(member["digest"], "record member digest")
            require_relative_path(member["path"], "record member path")
            if member["path"] != self._member_locator(member["digest"]):
                raise IntegrityError("record member locator differs from its digest")
            if member["path"] in paths:
                raise IntegrityError("record layer repeats a physical member")
            paths.add(member["path"])
            if member["mediaType"] != _MEDIA_TYPE or member["schemaId"] != schema.schema_id:
                raise IntegrityError("record member media type or schema differs from its layer")
            if member["byteSize"] > self.max_member_bytes:
                raise LimitExceededError(f"record member exceeds the {self.max_member_bytes}-byte limit")
            total += member["recordCount"]
        if isinstance(root["recordCount"], bool) or not isinstance(root["recordCount"], int) or total != root["recordCount"]:
            raise IntegrityError("record layer declared count differs from its members")
        return root, schema, policy

    def _admit_members(self, root: Mapping[str, Any], schema: RecordSchema) -> None:
        for member in root["members"]:
            path = _verified_member_path(
                self.root, member, media_type=_MEDIA_TYPE, schema_id=schema.schema_id,
                extra_fields=frozenset({"partition", "sequence", "identityMin", "identityMax"}),
            )
            try:
                with pq.ParquetFile(path) as parquet:
                    if not parquet.schema_arrow.equals(_physical_schema(schema), check_metadata=False):
                        raise IntegrityError("record member has an invalid physical Parquet schema")
                    if parquet.metadata.num_rows != member["recordCount"]:
                        raise IntegrityError("record member count differs from its description")
            except pa.ArrowException as error:
                raise IntegrityError("record member is not valid Parquet") from error

    def verify_members(self, reference: LayerRef) -> None:
        self._forget_admitted(reference)
        root, schema, _ = self._verified_root(reference)
        self._admit_members(root, schema)

    def verify(self, reference: LayerRef) -> None:
        self.admit(reference)

    @contextmanager
    def _relation(
        self, members: list[Mapping[str, Any]], schema: RecordSchema, policy: PartitionPolicy,
        *, partition_value: str | None = None, record_id: str | None = None,
        record_ids: list[str] | None = None,
        cursor=None, checked_paths=None, identity_ranges=None,
    ) -> Iterator[duckdb.DuckDBPyRelation]:
        if schema.identity_field == schema.partition_field:
            routing_ids = [record_id] if record_id is not None else record_ids
            if routing_ids is not None:
                selected = frozenset(self._bucket(identity, policy.bucket_count) for identity in routing_ids)
                members = [member for member in members if member["partition"] in selected]
        # Only already-admitted native readers may trust bounds for pruning.
        # Raw/full logical admission must see the rows that prove these bounds.
        if checked_paths is not None:
            if record_id is not None:
                members = _overlapping_members(members, ((record_id, record_id),))
            elif record_ids is not None:
                members = _overlapping_members(members, ((identity, identity) for identity in record_ids))
            if identity_ranges is not None:
                members = _overlapping_members(members, identity_ranges)
        paths = {checked_paths[member["path"]] if checked_paths is not None else
                 str(_contained(self.root, member["path"])): member for member in members}
        with (self._cursor() if cursor is None else nullcontext(cursor)) as cursor:
            if not paths:
                yield cursor.from_arrow(pa.Table.from_batches([], schema=pa.schema([*_physical_schema(schema), ("filename", pa.string())])))
            else:
                # Parameterized sql() eagerly materializes in DuckDB's Python
                # API. Keep this relation lazy so callers' field/key filters can
                # reach the Parquet scan before payload columns are read.
                relation = cursor.read_parquet(list(paths), filename=True, hive_partitioning=False)
                for name, value in (("partition_value", partition_value), ("record_identity", record_id)):
                    if value is not None:
                        relation = relation.filter(duckdb.ColumnExpression(name) == duckdb.ConstantExpression(value))
                if record_ids is not None:
                    if not record_ids:
                        relation = relation.filter("false")
                    else:
                        predicate = duckdb.ColumnExpression("record_identity").isin(
                            *(duckdb.ConstantExpression(identity) for identity in record_ids))
                        relation = relation.filter(predicate)
                        if len(paths) > 1:
                            # Discover matching files from compact identity columns
                            # before opening any large payload columns. IN filters
                            # alone may be optional Parquet filters, with no pruning.
                            selected = [row[0] for row in relation.project("filename").distinct().fetchall()]
                            if selected:
                                relation = cursor.read_parquet(selected, filename=True, hive_partitioning=False).filter(predicate)
                                if partition_value is not None:
                                    relation = relation.filter(duckdb.ColumnExpression("partition_value") == duckdb.ConstantExpression(partition_value))
                                if record_id is not None:
                                    relation = relation.filter(duckdb.ColumnExpression("record_identity") == duckdb.ConstantExpression(record_id))
                            else:
                                relation = relation.filter("false")
                yield relation.project(_column_list(schema) + ", filename")

    def _batches(self, members, schema, policy, *, partition_value=None, record_id=None, checked_paths=None):
        with self._relation(members, schema, policy, partition_value=partition_value, record_id=record_id,
                            checked_paths=checked_paths) as relation:
            with closing(relation.order("record_identity").to_arrow_reader(256)) as reader:
                yield from bounded_batches(reader, byte_column=tuple(_physical_schema(schema).names) if schema.columns else "record_json", max_value_bytes=self.max_record_bytes)

    def admit(self, reference: LayerRef) -> AdmittedRecordLayer:
        """Audit once before repeated native reads of immutable retained content.

        Keep this handle within the owning operation's content-protection lifetime.
        A fresh handle performs fresh physical and logical checks.
        """
        self._forget_admitted(reference)
        root, schema, policy = self._verified_root(reference)
        self._admit_members(root, schema)
        for _ in self._rows(root["members"], schema, policy):
            pass
        return self._remember_admitted(AdmittedRecordLayer(self, reference, root, schema, policy, self._checked_paths(root)))

    def _checked_paths(self, root):
        return {member["path"]: str(_contained(self.root, member["path"])) for member in root["members"]}

    def _rows(
        self, members: list[Mapping[str, Any]], schema: RecordSchema, policy: PartitionPolicy,
        *, partition_value: str | None = None, record_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not members:
            return
        paths = {str(_contained(self.root, member["path"])): member for member in members}
        counts = dict.fromkeys(paths, 0)
        previous: str | None = None
        with closing(self._batches(members, schema, policy, partition_value=partition_value, record_id=record_id)) as batches:
            for batch in batches:
                for physical in batch.to_pylist():
                    identity, value, payload, filename = (physical[field] for field in ("record_identity", "partition_value", "record_json", "filename"))
                    if not isinstance(payload, bytes):
                        raise IntegrityError("record member payload must be binary")
                    if len(payload) > self.max_record_bytes:
                        raise LimitExceededError(f"record exceeds the {self.max_record_bytes}-byte limit")
                    record = ({field: physical[field] for field in schema.fields} if schema.columns else
                              thaw_json(parse_canonical_json(payload, label="record member", file_form=False)))
                    if not isinstance(record, dict) or set(record) != set(schema.fields):
                        raise IntegrityError("record member row does not match its closed logical schema")
                    if (
                        identity != record_key(record[schema.identity_field], schema.identity_field)
                        or value != record_key(record[schema.partition_field], schema.partition_field)
                    ):
                        raise IntegrityError("record member routing columns differ from its logical row")
                    if previous is not None and identity <= previous:
                        raise IntegrityError("logical record identities are not globally unique and ordered")
                    previous = identity
                    member = paths[filename]
                    if not member["identityMin"] <= identity <= member["identityMax"]:
                        raise IntegrityError("record member identity lies outside its declared bounds")
                    if self._bucket(value, policy.bucket_count) != member["partition"]:
                        raise IntegrityError("logical record appears in the wrong partition")
                    counts[filename] += 1
                    if counts[filename] > member["recordCount"]:
                        raise IntegrityError("record member count differs from its description")
                    yield record
        if partition_value is None and record_id is None and any(counts[path] != member["recordCount"] for path, member in paths.items()):
            raise IntegrityError("record member count differs from its description")

    def stream(self, reference: LayerRef, *, partitions: frozenset[int] | None = None) -> Iterator[dict[str, Any]]:
        root, schema, policy = self._verified_root(reference)
        if partitions is not None and any(partition < 0 or partition >= policy.bucket_count for partition in partitions):
            raise ValueError("selected partition is outside the layer partition policy")
        yield from self._rows([
            member for member in root["members"] if partitions is None or member["partition"] in partitions
        ], schema, policy)

    def scan_partition_value(self, reference: LayerRef, partition_value: str) -> Iterator[dict[str, Any]]:
        record_key(partition_value, "partition_value")
        root, schema, policy = self._verified_root(reference)
        bucket = self._bucket(partition_value, policy.bucket_count)
        yield from self._rows([member for member in root["members"] if member["partition"] == bucket], schema, policy, partition_value=partition_value)

    def lookup(self, reference: LayerRef, record_id: str, *, partition_value: str | None = None) -> dict[str, Any] | None:
        record_key(record_id, "record_id")
        root, schema, policy = self._verified_root(reference)
        members = root["members"]
        if partition_value is not None:
            record_key(partition_value, "partition_value")
            bucket = self._bucket(partition_value, policy.bucket_count)
            members = [member for member in members if member["partition"] == bucket]
        result = None
        for row in self._rows(members, schema, policy, partition_value=partition_value, record_id=record_id):
            result = row
        return result

    def identity_field(self, reference: LayerRef) -> str:
        return self.schema(reference).identity_field

    def lookup_batches(self, reference: LayerRef, record_ids: Iterable[str]) -> Iterator[pa.RecordBatch]:
        admitted = self.admitted(reference)
        # Bound both query parameters and native results. Callers hold the
        # admission/protection scope; this query never parses payload bytes.
        with owned_iterator(bounded_rows(record_ids, size=lambda key: len(record_key(key, "record_id").encode("utf-8")))) as chunks:
            for chunk in chunks:
                keys = list(chunk)
                for key in keys:
                    record_key(key, "record_id")
                with self._relation(admitted._root["members"], admitted.schema, admitted.partition_policy,
                                    record_ids=keys, checked_paths=admitted._paths) as relation:
                    with closing(relation.order("record_identity").to_arrow_reader(256)) as reader:
                        for batch in bounded_batches(reader, byte_column=tuple(_physical_schema(admitted.schema).names) if admitted.schema.columns else "record_json", max_value_bytes=self.max_record_bytes):
                            yield batch.select(_physical_schema(admitted.schema).names)

    @contextmanager
    def relations(self, references: Mapping[str, LayerRef | AdmittedRecordLayer], *, partitions=None, tables=None, identities=None, identity_ranges=None, cursor=None):
        """Read unordered admitted layers on one connection for native joins."""
        with (self._cursor() if cursor is None else nullcontext(cursor)) as cursor, ExitStack() as stack:
            result = {}
            for name, reference in references.items():
                if isinstance(reference, AdmittedRecordLayer):
                    admitted = reference
                    if admitted._storage is not self:
                        raise IntegrityError("admitted layer belongs to another record store")
                else:
                    admitted = self.admitted(reference)
                selected = None if partitions is None else partitions.get(name)
                result[name] = stack.enter_context(self._relation(admitted._members(selected), admitted.schema, admitted.partition_policy, cursor=cursor,
                                                                  record_ids=None if identities is None else identities.get(name), checked_paths=admitted._paths,
                                                                  identity_ranges=None if identity_ranges is None else identity_ranges.get(name)))
            for name, table in (tables or {}).items():
                if name in result:
                    raise IntegrityError("native input names must be distinct")
                result[name] = cursor.from_arrow(table)
            yield result

    def available(self, reference: LayerRef) -> AdmittedRecordLayer:
        """Check admitted immutable member addresses and sizes without a byte scan."""
        self._forget_admitted(reference)
        root, schema, policy = self._verified_root(reference)
        paths = _available_paths(self.root, ((member["path"], member["byteSize"]) for member in root["members"]))
        return self._remember_admitted(AdmittedRecordLayer(self, reference, root, schema, policy, paths))

    def union_disjoint(self, base: AdmittedRecordLayer, changes: AdmittedRecordLayer, *, exclude_existing=False) -> AdmittedRecordLayer:
        """Share admitted files after checking disjoint logical identities.

        Only identity columns are scanned; payloads remain in their original files.
        Excluding existing identities preserves the base rows at those identities.
        """
        if (base._storage is not self or changes._storage is not self or base.schema != changes.schema
                or base.partition_policy != changes.partition_policy or base.reference.layer_kind != changes.reference.layer_kind):
            raise IntegrityError("record union requires compatible admitted layers")
        if not changes.reference.record_count:
            return base
        partitions = None
        if base.schema.identity_field == base.schema.partition_field:
            partitions = {"base": frozenset(member["partition"] for member in changes._root["members"])}
        ranges = {"base": ((member["identityMin"], member["identityMax"]) for member in changes._root["members"])}
        with self.relations({"base": base, "changes": changes}, partitions=partitions, identity_ranges=ranges) as relations:
            existing = relations["base"].project("record_identity AS existing_key")
            incoming = relations["changes"].project("record_identity")
            if incoming.join(existing, "record_identity = existing_key", how="semi").limit(1).fetchone():
                if not exclude_existing:
                    raise IntegrityError("record union requires disjoint logical identities")
                added = relations["changes"].project(_column_list(changes.schema)).join(
                    existing, "record_identity = existing_key", how="anti").order("record_identity")
                with closing(added.to_arrow_reader(256)) as batches:
                    changes = self.retain_batches(batches, layer_kind=changes.reference.layer_kind,
                                                  schema=changes.schema, partition_policy=changes.partition_policy)
        if not changes.reference.record_count:
            return base
        members, sequences = [], {}
        for member in sorted((*base._root["members"], *changes._root["members"]), key=lambda member: member["partition"]):
            partition = member["partition"]
            sequence = sequences.get(partition, 0)
            members.append({**member, "sequence": sequence})
            sequences[partition] = sequence + 1
        return self._retain_root(members, layer_kind=base.reference.layer_kind, schema=base.schema, partition_policy=base.partition_policy)

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
        return self._verified_root(reference)[1]

    def physical_references(self, reference: LayerRef) -> Iterator[BlobRef]:
        root = self._verified_root(reference)[0]
        for member in root["members"]:
            yield BlobRef(member["path"], member["digest"], member["byteSize"], member["mediaType"])
        yield BlobRef(reference.state_ref, reference.digest, _contained(self.root, reference.state_ref).stat().st_size, "application/json")

    def delete(self, reference: BlobRef) -> bool:
        hexadecimal = require_sha256(reference.digest).removeprefix("sha256:")
        roots = {self._member_locator(reference.digest), f"record-layers/sha256/{hexadecimal[:2]}/{hexadecimal}.json"}
        if reference.locator not in roots:
            raise IntegrityError("record content locator differs from its digest")
        return delete_content(self.root, reference)

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy:
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

    def _write_batches(
        self, batches, *, layer_kind, schema, partition_policy, base, replace_partitions, ordered=True,
        target_member_bytes=None,
    ) -> AdmittedRecordLayer:
        require_text(layer_kind, "layer_kind")
        physical_schema = _physical_schema(schema)
        write_schema = pa.schema([("partition", pa.int32()), *physical_schema])
        target_member_bytes = self.max_member_bytes // 2 if target_member_bytes is None else target_member_bytes
        if type(target_member_bytes) is not int or not 0 < target_member_bytes <= self.max_member_bytes:
            raise ValueError("target member bytes must be positive and within the member limit")
        members: dict[tuple[int, int], dict[str, Any]] = {}
        if base is None:
            if replace_partitions is not None:
                raise ValueError("replace_partitions requires a base layer")
        else:
            if replace_partitions is None:
                raise ValueError("an incremental layer requires replace_partitions")
            if any(partition < 0 or partition >= partition_policy.bucket_count for partition in replace_partitions):
                raise ValueError("replacement partition is outside the layer partition policy")
            if isinstance(base, AdmittedRecordLayer):
                if base._storage is not self:
                    raise IntegrityError("incremental base belongs to another record store")
                root, base_schema, policy = base._root, base.schema, base.partition_policy
                reference = base.reference
            else:
                admitted = self.available(base)
                root, base_schema, policy = admitted._root, admitted.schema, admitted.partition_policy
                reference = base
            if reference.layer_kind != layer_kind or base_schema != schema or policy != partition_policy:
                raise IntegrityError("incremental layer is incompatible with its base")
            members = {(member["partition"], member["sequence"]): member for member in root["members"] if member["partition"] not in replace_partitions}
        record_count = 0
        source_error: BaseException | None = None

        def writing_batches() -> Iterator[pa.RecordBatch]:
            nonlocal record_count, source_error
            previous: str | None = None
            try:
                with owned_iterator(batches) as source:
                    for batch in source:
                        if not batch.schema.equals(physical_schema, check_metadata=False):
                            raise IntegrityError("record batch has an invalid physical schema")
                        if any(batch.column(name).null_count for name in _PARQUET_SCHEMA.names):
                            raise IntegrityError("record batch columns must not contain nulls")
                        with closing(bounded_batches([batch], byte_column=tuple(physical_schema.names) if schema.columns else "record_json",
                                                     max_value_bytes=self.max_record_bytes)) as bounded:
                            for part in bounded:
                                partitions = []
                                lengths = pc.binary_length(part.column(0)).cast(pa.int64())
                                for column in part.columns[1:]:
                                    lengths = pc.add(lengths, pc.fill_null(pc.binary_length(column), 0))
                                row_sizes = lengths.to_pylist()
                                for identity, value, size in zip(
                                    part.column("record_identity").to_pylist(),
                                    part.column("partition_value").to_pylist(), row_sizes, strict=True,
                                ):
                                    if ordered and previous is not None and identity <= previous:
                                        raise IntegrityError("record input must be strictly ordered by logical identity")
                                    previous = identity
                                    partition = self._bucket(value, partition_policy.bucket_count)
                                    if replace_partitions is not None and partition not in replace_partitions:
                                        raise IntegrityError("incremental records include a partition not declared for replacement")
                                    if size > self.max_member_bytes:
                                        raise LimitExceededError(f"record exceeds the {self.max_member_bytes}-byte member limit")
                                    partitions.append(partition)
                                record_count += part.num_rows
                                yield pa.RecordBatch.from_arrays([
                                    pa.array(partitions, type=pa.int32()), *part.columns,
                                ], schema=write_schema)
            except BaseException as error:
                # Arrow transports callback failures through a native exception.
                # Preserve the original producer/budget error, without parsing it.
                if not isinstance(error, GeneratorExit):
                    source_error = error
                raise

        with (
            self._cursor() as cursor,
            tempfile.TemporaryDirectory(prefix="records-", dir=self._staging) as directory,
            closing(writing_batches()) as source_batches,
            closing(pa.RecordBatchReader.from_batches(write_schema, source_batches)) as reader,
        ):
            output_directory = Path(directory)
            completed = []
            writer = None
            partition = None
            sequence = count = file_bytes = group_bytes = group_rows = 0
            lower = upper = None
            pending = []
            # Small byte-bounded groups provide selective reads independently
            # of file packing. A large single record forms its own group.
            group_target = min(384 * 1024, target_member_bytes)

            def flush_group():
                nonlocal group_bytes, group_rows
                if pending:
                    writer.write_table(pa.Table.from_batches(pending, schema=physical_schema), row_group_size=group_rows)
                    pending.clear()
                    group_bytes = group_rows = 0

            def finish_file():
                nonlocal writer
                if writer is not None:
                    flush_group()
                    writer.close()
                    completed.append((partition, sequence, temporary, count, lower, upper))
                    writer = None

            try:
                # Consume once, sort natively, then choose tight file/group
                # boundaries. Arrow slices retain bytes without Python decoding.
                cursor.register("writing_records", reader.__arrow_c_stream__())
                with closing(cursor.sql("SELECT * FROM writing_records ORDER BY partition, record_identity").to_arrow_reader(256)) as sorted_rows:
                    for batch in sorted_rows:
                        physical = batch.select(physical_schema.names)
                        lengths = pc.binary_length(physical.column(0)).cast(pa.int64())
                        for column in physical.columns[1:]:
                            lengths = pc.add(lengths, pc.fill_null(pc.binary_length(column), 0))
                        start = 0
                        for index, (bucket, identity, size) in enumerate(zip(batch.column("partition").to_pylist(),
                                physical.column("record_identity").to_pylist(), lengths.to_pylist(), strict=True)):
                            new_file = partition != bucket or (file_bytes and file_bytes + size > target_member_bytes)
                            new_group = group_rows and (group_bytes + size > group_target or group_rows >= BATCH_ROWS)
                            if new_file or new_group:
                                if index > start:
                                    pending.append(physical.slice(start, index - start))
                                start = index
                                if new_file:
                                    finish_file()
                                    sequence = sequence + 1 if partition == bucket else 0
                                    partition, count, file_bytes = bucket, 0, 0
                                else:
                                    flush_group()
                            if writer is None:
                                temporary = output_directory / f"{bucket}-{sequence}.parquet"
                                writer = pq.ParquetWriter(temporary, physical_schema, compression="zstd")
                                lower = identity
                            upper = identity
                            count += 1
                            file_bytes += size
                            group_bytes += size
                            group_rows += 1
                        if start < batch.num_rows:
                            pending.append(physical.slice(start))
                    finish_file()
            except BaseException:
                if source_error is not None:
                    raise source_error
                raise
            finally:
                if writer is not None:
                    writer.close()
            if not ordered and record_count:
                # Validate global uniqueness before any file is published.
                if cursor.execute(
                    "SELECT 1 FROM read_parquet(?,hive_partitioning=false) GROUP BY record_identity HAVING count(*)>1 LIMIT 1",
                    [[str(item[2]) for item in completed]],
                ).fetchone():
                    raise IntegrityError("record input contains duplicate logical identities")
            written_count = 0
            for partition, sequence, temporary, count, lower, upper in completed:
                _sync_file(temporary)
                digest, byte_size = sha256_file(temporary)
                if byte_size > self.max_member_bytes:
                    raise LimitExceededError(f"record member exceeds the {self.max_member_bytes}-byte limit")
                locator = self._member_locator(digest)
                _link_content(self.root, temporary, locator, digest, byte_size)
                temporary.unlink()
                members[(partition, sequence)] = {
                    "partition": partition, "sequence": sequence, "path": locator,
                    "mediaType": _MEDIA_TYPE, "byteSize": byte_size, "digest": digest,
                    "recordCount": count, "schemaId": schema.schema_id,
                    "identityMin": lower, "identityMax": upper,
                }
                written_count += count
            if written_count != record_count:
                raise IntegrityError("native record writer count differs from its input")
        return self._retain_root([members[key] for key in sorted(members)], layer_kind=layer_kind,
                                 schema=schema, partition_policy=partition_policy)

    def _retain_root(self, members, *, layer_kind, schema, partition_policy) -> AdmittedRecordLayer:
        """Publish one root over physically admitted member references."""
        content = {
            "layerKind": layer_kind, "schema": self._schema_dict(schema), "profileId": _PROFILE_ID,
            "partitionPolicy": self._policy_dict(partition_policy), "members": members,
            "recordCount": sum(member["recordCount"] for member in members),
        }
        layer_id = stable_urn("record-layer", content)
        root = {"format": "docspec-record-layer", "formatVersion": "4.0", "layerId": layer_id, **content}
        payload = canonical_json_file_bytes(root)
        if len(payload) > self.max_root_bytes:
            raise LimitExceededError(f"record layer root exceeds the {self.max_root_bytes}-byte limit")
        digest = sha256_digest(payload)
        locator = f"record-layers/sha256/{digest[7:9]}/{digest[7:]}.json"
        _write_once(self.root, locator, payload)
        reference = LayerRef(layer_id, layer_kind, schema.schema_id, _PROFILE_ID, locator, digest, content["recordCount"])
        # These members were just written or carried by an admitted base. Their
        # contained paths were checked by those owners within this protection.
        paths = {member["path"]: str(self.root / member["path"]) for member in members}
        return self._remember_admitted(AdmittedRecordLayer(self, reference, root, schema, partition_policy, paths))


@dataclass(frozen=True, slots=True)
class AdmittedRecordLayer:
    """An operation-scoped admission of immutable physical and logical records."""

    _storage: LocalParquetRecordStorage
    reference: LayerRef
    _root: Mapping[str, Any]
    schema: RecordSchema
    partition_policy: PartitionPolicy
    _paths: Mapping[str, str]

    def _members(self, partitions):
        if partitions is not None and any(
            partition < 0 or partition >= self.partition_policy.bucket_count for partition in partitions
        ):
            raise ValueError("selected partition is outside the layer partition policy")
        return [member for member in self._root["members"] if partitions is None or member["partition"] in partitions]

    @contextmanager
    def relation(self, *, partitions: frozenset[int] | None = None) -> Iterator[duckdb.DuckDBPyRelation]:
        """Read unordered rows for native joins inside the protection scope."""
        with self._storage._relation(self._members(partitions), self.schema, self.partition_policy, checked_paths=self._paths) as relation:
            yield relation.project(_column_list(self.schema))

    def batches(self, *, partitions: frozenset[int] | None = None) -> Iterator[pa.RecordBatch]:
        """Stream admitted canonical bytes without another JSON conversion."""
        with closing(self._storage._batches(self._members(partitions), self.schema, self.partition_policy, checked_paths=self._paths)) as batches:
            for batch in batches:
                yield batch.select(_physical_schema(self.schema).names)
