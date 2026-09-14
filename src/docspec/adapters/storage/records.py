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

from docspec.ports.record_storage import bounded_batches, bounded_rows
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

_PROFILE_ID = "urn:docspec:profile:record-storage:local-parquet:2"
_MEDIA_TYPE = "application/vnd.apache.parquet"
_ADMITTED_LAYER_LIMIT = 8
_PARQUET_SCHEMA = ENCODED_RECORD_SCHEMA
_WRITE_SCHEMA = pa.schema([
    ("partition", pa.int32()), ("sequence", pa.int64()), *_PARQUET_SCHEMA,
])


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
        }

    @staticmethod
    def _policy_dict(policy: PartitionPolicy) -> dict[str, Any]:
        return {"policyId": policy.policy_id, "bucketCount": policy.bucket_count}

    def _verified_root(self, reference: LayerRef) -> tuple[dict[str, Any], RecordSchema, PartitionPolicy]:
        root = self._load_root(reference)
        if (
            set(root) != {"format", "formatVersion", "layerId", "layerKind", "schema", "profileId", "partitionPolicy", "members", "recordCount"}
            or root["format"] != "docspec-record-layer" or root["formatVersion"] != "3.0"
        ):
            raise IntegrityError("record layer root has an unknown format or invalid closed shape")
        value = root["schema"]
        if not isinstance(value, dict) or set(value) != {"schemaId", "fields", "identityField", "partitionField"} or not isinstance(value["fields"], list):
            raise IntegrityError("record layer schema has an invalid closed shape")
        schema = RecordSchema(value["schemaId"], tuple(value["fields"]), value["identityField"], value["partitionField"])
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
                    if not parquet.schema_arrow.equals(_PARQUET_SCHEMA, check_metadata=False):
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
                yield cursor.sql(
                    "SELECT NULL::VARCHAR record_identity, NULL::VARCHAR partition_value, "
                    "NULL::BLOB record_json, NULL::VARCHAR filename WHERE false"
                )
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
                yield relation.project("record_identity, partition_value, record_json, filename")

    def _batches(self, members, schema, policy, *, partition_value=None, record_id=None, checked_paths=None):
        with self._relation(members, schema, policy, partition_value=partition_value, record_id=record_id,
                            checked_paths=checked_paths) as relation:
            with closing(relation.order("record_identity").to_arrow_reader(256)) as reader:
                yield from bounded_batches(reader, byte_column="record_json", max_value_bytes=self.max_record_bytes)

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
                    identity, value, payload, filename = (physical[field] for field in batch.schema.names)
                    if not isinstance(payload, bytes):
                        raise IntegrityError("record member payload must be binary")
                    if len(payload) > self.max_record_bytes:
                        raise LimitExceededError(f"record exceeds the {self.max_record_bytes}-byte limit")
                    record = thaw_json(parse_canonical_json(payload, label="record member", file_form=False))
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
                        for batch in bounded_batches(reader, byte_column="record_json", max_value_bytes=self.max_record_bytes):
                            yield batch.select(_PARQUET_SCHEMA.names)

    @contextmanager
    def relations(self, references: Mapping[str, LayerRef | AdmittedRecordLayer], *, partitions=None, tables=None, identities=None, identity_ranges=None):
        """Read unordered admitted layers on one connection for native joins."""
        with self._cursor() as cursor, ExitStack() as stack:
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
                added = relations["changes"].project("record_identity, partition_value, record_json").join(
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
            before = relations["before"].project("record_identity AS old_key, partition_value AS old_partition, record_json AS old_payload")
            after = relations["after"].project("record_identity AS new_key, partition_value AS new_partition, record_json AS new_payload")
            if before.join(after, "old_key = new_key", how="outer").filter(
                "old_key IS NULL OR new_key IS NULL OR old_partition IS DISTINCT FROM new_partition OR old_payload IS DISTINCT FROM new_payload"
            ).limit(1).fetchone():
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
                    yield (
                        record_key(record[schema.identity_field], schema.identity_field),
                        record_key(record[schema.partition_field], schema.partition_field),
                        canonical_json_bytes(record),
                    )

        return self.write_batches(
            encoded_batches(rows(), _PARQUET_SCHEMA, byte_column=2, max_value_bytes=self.max_record_bytes),
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
        sizes: dict[int, int] = {}
        sequences: dict[int, int] = {}
        bounds: dict[tuple[int, int], tuple[str, str]] = {}
        record_count = 0
        source_error: BaseException | None = None

        def writing_batches() -> Iterator[pa.RecordBatch]:
            nonlocal record_count, source_error
            previous: str | None = None
            try:
                with owned_iterator(batches) as source:
                    for batch in source:
                        if not batch.schema.equals(_PARQUET_SCHEMA, check_metadata=False):
                            raise IntegrityError("record batch has an invalid physical schema")
                        if any(column.null_count for column in batch.columns):
                            raise IntegrityError("record batch columns must not contain nulls")
                        with closing(bounded_batches([batch], byte_column="record_json",
                                                     max_value_bytes=self.max_record_bytes)) as bounded:
                            for part in bounded:
                                partitions, shard_sequences = [], []
                                row_sizes = pc.add(
                                    pc.add(pc.binary_length(part.column("record_identity")).cast(pa.int64()),
                                           pc.binary_length(part.column("partition_value"))),
                                    pc.binary_length(part.column("record_json")),
                                ).to_pylist()
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
                                    sequence = sequences.get(partition, 0)
                                    if sizes.get(partition, 0) and sizes[partition] + size > target_member_bytes:
                                        sequence += 1
                                        sequences[partition] = sequence
                                        sizes[partition] = 0
                                    sizes[partition] = sizes.get(partition, 0) + size
                                    shard = partition, sequence
                                    lower, upper = bounds.get(shard, (identity, identity))
                                    bounds[shard] = (lower, identity) if ordered else (min(lower, identity), max(upper, identity))
                                    partitions.append(partition)
                                    shard_sequences.append(sequence)
                                record_count += part.num_rows
                                yield pa.RecordBatch.from_arrays([
                                    pa.array(partitions, type=pa.int32()),
                                    pa.array(shard_sequences, type=pa.int64()), *part.columns,
                                ], schema=_WRITE_SCHEMA)
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
            closing(pa.RecordBatchReader.from_batches(_WRITE_SCHEMA, source_batches)) as reader,
        ):
            output_directory = Path(directory) / "members"
            output_path = str(output_directory).replace("'", "''")
            try:
                # The direct C stream avoids DuckDB's threaded Arrow scanner,
                # preserving caller-thread input consumption in the pinned backend.
                cursor.register("writing_records", reader.__arrow_c_stream__())
                cursor.execute(
                    "COPY (SELECT * FROM writing_records "
                    "ORDER BY partition, sequence, partition_value, record_identity) "
                    f"TO '{output_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048, "
                    "PARTITION_BY (partition, sequence), WRITE_PARTITION_COLUMNS false, RETURN_STATS)",
                )
            except BaseException:
                if source_error is not None:
                    raise source_error
                raise
            if not ordered and record_count:
                # Import streams need not arrive in key order. Check uniqueness
                # natively before publishing any physical member, reading only
                # the identity column from the writer's completed temporary files.
                with self._cursor() as checker:
                    if checker.execute(
                        "SELECT 1 FROM read_parquet(?,hive_partitioning=false) GROUP BY record_identity HAVING count(*)>1 LIMIT 1",
                        [[str(path) for path in output_directory.rglob("*.parquet")]],
                    ).fetchone():
                        raise IntegrityError("record input contains duplicate logical identities")
            written_count = 0
            while sizes and (output := cursor.fetchone()) is not None:
                filename, count = output[:2]
                temporary = Path(filename)
                parts = temporary.relative_to(output_directory).parts
                if len(parts) != 3 or not parts[0].startswith("partition=") or not parts[1].startswith("sequence="):
                    raise IntegrityError("native record writer produced an unexpected member path")
                partition, sequence = int(parts[0][10:]), int(parts[1][9:])
                if partition not in sizes or not 0 <= sequence <= sequences.get(partition, 0) or (partition, sequence) in members:
                    raise IntegrityError("native record writer produced an unexpected partition shard")
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
                    "identityMin": bounds[(partition, sequence)][0], "identityMax": bounds[(partition, sequence)][1],
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
        root = {"format": "docspec-record-layer", "formatVersion": "3.0", "layerId": layer_id, **content}
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
            yield relation.project("record_identity, partition_value, record_json")

    def batches(self, *, partitions: frozenset[int] | None = None) -> Iterator[pa.RecordBatch]:
        """Stream admitted canonical bytes without another JSON conversion."""
        with closing(self._storage._batches(self._members(partitions), self.schema, self.partition_policy, checked_paths=self._paths)) as batches:
            for batch in batches:
                yield batch.select(_PARQUET_SCHEMA.names)
