"""Immutable Parquet record layers, queried and sorted by DuckDB."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from docspec.adapters.storage.files import (
    _contained,
    _read_exact,
    _storage_root,
    _sync_file,
    _verified_member_path,
    _write_once,
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
from docspec.domain.references import LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from docspec.errors import IntegrityError, LimitExceededError

_PROFILE_ID = "urn:docspec:profile:record-storage:local-parquet:1"
_MEDIA_TYPE = "application/vnd.apache.parquet"
_PARQUET_SCHEMA = pa.schema([
    ("record_identity", pa.string()),
    ("partition_value", pa.string()),
    ("record_json", pa.binary()),
])
_WRITE_SCHEMA = pa.schema([
    ("partition", pa.int32()), ("sequence", pa.int64()), *_PARQUET_SCHEMA,
])
_BATCH_ROWS = 2048


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
        merge_scratch_root: Path | None = None,
        create: bool = True,
    ) -> None:
        if min(max_member_bytes, max_record_bytes, max_root_bytes, max_merge_scratch_bytes) <= 0:
            raise ValueError("record storage limits must be positive")
        self.root = _storage_root(root, create=create)
        self.max_member_bytes = max_member_bytes
        self.max_record_bytes = max_record_bytes
        self.max_root_bytes = max_root_bytes
        self.max_merge_scratch_bytes = max_merge_scratch_bytes
        self.merge_scratch_root = (
            None if merge_scratch_root is None else _storage_root(merge_scratch_root, create=create)
        )
        # The routing fields can be the same logical field. Its UTF-8 string
        # then occurs twice beside the row, requiring up to three row allowances.
        self._batch_bytes = max(3 * max_record_bytes, min(max_member_bytes, 8 * 1024**2))
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._connection_lock = Lock()
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
                    self._connection = duckdb.connect(config={
                        "threads": "1",
                        "memory_limit": f"{max(128 * 1024**2, self.max_member_bytes, 2 * self.max_record_bytes)}B",
                        "temp_directory": scratch.name,
                        "max_temp_directory_size": f"{self.max_merge_scratch_bytes}B",
                        "preserve_insertion_order": "false",
                        "partitioned_write_max_open_files": "1",
                    })
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
            or root["format"] != "docspec-record-layer" or root["formatVersion"] != "2.0"
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
            if not isinstance(member, dict) or set(member) != {"partition", "sequence", "path", "mediaType", "byteSize", "digest", "recordCount", "schemaId"}:
                raise IntegrityError("record layer member has an invalid closed shape")
            for name in ("partition", "sequence", "byteSize", "recordCount"):
                if not isinstance(member[name], int) or isinstance(member[name], bool) or member[name] < 0:
                    raise IntegrityError("record member counts and positions must be non-negative integers")
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
                extra_fields=frozenset({"partition", "sequence"}),
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
        root, schema, _ = self._verified_root(reference)
        self._admit_members(root, schema)

    def verify(self, reference: LayerRef) -> None:
        root, schema, policy = self._verified_root(reference)
        self._admit_members(root, schema)
        for _ in self._rows(root["members"], schema, policy):
            pass

    def _rows(
        self, members: list[Mapping[str, Any]], schema: RecordSchema, policy: PartitionPolicy,
        *, partition_value: str | None = None, record_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not members:
            return
        paths = {str(_contained(self.root, member["path"])): member for member in members}
        predicates: list[str] = []
        parameters: list[Any] = [list(paths)]
        if partition_value is not None:
            predicates.append("partition_value = ?")
            parameters.append(partition_value)
        if record_id is not None:
            predicates.append("record_identity = ?")
            parameters.append(record_id)
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        counts = dict.fromkeys(paths, 0)
        previous: str | None = None
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT record_identity, partition_value, record_json, filename "
                "FROM read_parquet(?, filename=true, hive_partitioning=false)" + where + " ORDER BY record_identity",
                parameters,
            )
            batch_rows = max(1, min(_BATCH_ROWS, self._batch_bytes // (3 * self.max_record_bytes)))
            while batch := cursor.fetchmany(batch_rows):
                for identity, value, payload, filename in batch:
                    if not isinstance(payload, bytes):
                        raise IntegrityError("record member payload must be binary")
                    if len(payload) > self.max_record_bytes:
                        raise LimitExceededError(f"record exceeds the {self.max_record_bytes}-byte limit")
                    record = thaw_json(parse_canonical_json(payload, label="record member", file_form=False))
                    if not isinstance(record, dict) or set(record) != set(schema.fields):
                        raise IntegrityError("record member row does not match its closed logical schema")
                    if (
                        identity != require_text(record[schema.identity_field], schema.identity_field)
                        or value != require_text(record[schema.partition_field], schema.partition_field)
                    ):
                        raise IntegrityError("record member routing columns differ from its logical row")
                    if previous is not None and identity <= previous:
                        raise IntegrityError("logical record identities are not globally unique and ordered")
                    previous = identity
                    member = paths[filename]
                    if self._bucket(value, policy.bucket_count) != member["partition"]:
                        raise IntegrityError("logical record appears in the wrong partition")
                    counts[filename] += 1
                    if counts[filename] > member["recordCount"]:
                        raise IntegrityError("record member count differs from its description")
                    yield record
        if not predicates and any(counts[path] != member["recordCount"] for path, member in paths.items()):
            raise IntegrityError("record member count differs from its description")

    def stream(self, reference: LayerRef, *, partitions: frozenset[int] | None = None) -> Iterator[dict[str, Any]]:
        root, schema, policy = self._verified_root(reference)
        if partitions is not None and any(partition < 0 or partition >= policy.bucket_count for partition in partitions):
            raise ValueError("selected partition is outside the layer partition policy")
        yield from self._rows([
            member for member in root["members"] if partitions is None or member["partition"] in partitions
        ], schema, policy)

    def scan_partition_value(self, reference: LayerRef, partition_value: str) -> Iterator[dict[str, Any]]:
        require_text(partition_value, "partition_value")
        root, schema, policy = self._verified_root(reference)
        bucket = self._bucket(partition_value, policy.bucket_count)
        yield from self._rows([member for member in root["members"] if member["partition"] == bucket], schema, policy, partition_value=partition_value)

    def lookup(self, reference: LayerRef, record_id: str, *, partition_value: str | None = None) -> dict[str, Any] | None:
        require_text(record_id, "record_id")
        root, schema, policy = self._verified_root(reference)
        members = root["members"]
        if partition_value is not None:
            require_text(partition_value, "partition_value")
            bucket = self._bucket(partition_value, policy.bucket_count)
            members = [member for member in members if member["partition"] == bucket]
        result = None
        for row in self._rows(members, schema, policy, partition_value=partition_value, record_id=record_id):
            result = row
        return result

    def identity_field(self, reference: LayerRef) -> str:
        return self.schema(reference).identity_field

    def schema(self, reference: LayerRef) -> RecordSchema:
        return self._verified_root(reference)[1]

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy:
        return self._verified_root(reference)[2]

    def write_layer(
        self, records: Iterable[Mapping[str, Any]], *, layer_kind: str, schema: RecordSchema,
        partition_policy: PartitionPolicy, base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef:
        """Validate new rows and copy compatible pinned base references.

        Copying an untouched member reference does not audit its current bytes;
        consumers admit inherited rows, and verify/retention audits the result.
        """
        require_text(layer_kind, "layer_kind")
        members: dict[tuple[int, int], dict[str, Any]] = {}
        if base is None:
            if replace_partitions is not None:
                raise ValueError("replace_partitions requires a base layer")
        else:
            if replace_partitions is None:
                raise ValueError("an incremental layer requires replace_partitions")
            if any(partition < 0 or partition >= partition_policy.bucket_count for partition in replace_partitions):
                raise ValueError("replacement partition is outside the layer partition policy")
            root, base_schema, policy = self._verified_root(base)
            if base.layer_kind != layer_kind or base_schema != schema or policy != partition_policy:
                raise IntegrityError("incremental layer is incompatible with its base")
            members = {(member["partition"], member["sequence"]): member for member in root["members"] if member["partition"] not in replace_partitions}
        sizes: dict[int, int] = {}
        sequences: dict[int, int] = {}
        record_count = 0
        source_error: BaseException | None = None

        def batches() -> Iterator[pa.RecordBatch]:
            nonlocal record_count, source_error
            batch: list[tuple[int, int, str, str, bytes]] = []
            batch_bytes = 0
            previous: str | None = None

            def arrow_batch() -> pa.RecordBatch:
                return pa.RecordBatch.from_arrays([
                    pa.array(column, type=field.type)
                    for column, field in zip(zip(*batch), _WRITE_SCHEMA, strict=True)
                ], schema=_WRITE_SCHEMA)

            try:
                source = iter(records)
                try:
                    for record in source:
                        if set(record) != set(schema.fields):
                            raise IntegrityError("record does not match its closed logical schema")
                        identity = require_text(record[schema.identity_field], schema.identity_field)
                        value = require_text(record[schema.partition_field], schema.partition_field)
                        if previous is not None and identity <= previous:
                            raise IntegrityError("record input must be strictly ordered by logical identity")
                        previous = identity
                        partition = self._bucket(value, partition_policy.bucket_count)
                        if replace_partitions is not None and partition not in replace_partitions:
                            raise IntegrityError("incremental records include a partition not declared for replacement")
                        payload = canonical_json_bytes(record)
                        if len(payload) > self.max_record_bytes:
                            raise LimitExceededError(f"record exceeds the {self.max_record_bytes}-byte limit")
                        size = len(payload) + len(identity.encode("utf-8")) + len(value.encode("utf-8"))
                        if size > self.max_member_bytes:
                            raise LimitExceededError(f"record exceeds the {self.max_member_bytes}-byte member limit")
                        sequence = sequences.get(partition, 0)
                        if sizes.get(partition, 0) and sizes[partition] + size > self.max_member_bytes // 2:
                            sequence += 1
                            sequences[partition] = sequence
                            sizes[partition] = 0
                        record_count += 1
                        sizes[partition] = sizes.get(partition, 0) + size
                        if batch and (len(batch) >= _BATCH_ROWS or batch_bytes + size > self._batch_bytes):
                            yield arrow_batch()
                            batch.clear()
                            batch_bytes = 0
                        batch.append((partition, sequence, identity, value, payload))
                        batch_bytes += size
                    if batch:
                        yield arrow_batch()
                finally:
                    close_source = getattr(source, "close", None)
                    if close_source is not None:
                        close_source()
            except BaseException as error:
                # Arrow transports callback failures through a native exception.
                # Preserve the actual producer/budget error, without parsing it.
                if not isinstance(error, GeneratorExit):
                    source_error = error
                raise

        with (
            self._cursor() as cursor,
            tempfile.TemporaryDirectory(prefix="records-", dir=self._staging) as directory,
            closing(batches()) as source_batches,
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
                destination = _contained(self.root, locator, create_parents=True)
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if destination.is_symlink() or not destination.is_file() or sha256_file(destination) != (digest, byte_size):
                        raise IntegrityError("record member conflicts with an existing immutable object") from None
                temporary.unlink()
                members[(partition, sequence)] = {
                    "partition": partition, "sequence": sequence, "path": locator,
                    "mediaType": _MEDIA_TYPE, "byteSize": byte_size, "digest": digest,
                    "recordCount": count, "schemaId": schema.schema_id,
                }
                written_count += count
            if written_count != record_count:
                raise IntegrityError("native record writer count differs from its input")
        ordered = [members[key] for key in sorted(members)]
        content = {
            "layerKind": layer_kind, "schema": self._schema_dict(schema), "profileId": _PROFILE_ID,
            "partitionPolicy": self._policy_dict(partition_policy), "members": ordered,
            "recordCount": sum(member["recordCount"] for member in ordered),
        }
        layer_id = stable_urn("record-layer", content)
        payload = canonical_json_file_bytes({"format": "docspec-record-layer", "formatVersion": "2.0", "layerId": layer_id, **content})
        if len(payload) > self.max_root_bytes:
            raise LimitExceededError(f"record layer root exceeds the {self.max_root_bytes}-byte limit")
        digest = sha256_digest(payload)
        locator = f"record-layers/sha256/{digest[7:9]}/{digest[7:]}.json"
        _write_once(self.root, locator, payload)
        return LayerRef(layer_id, layer_kind, schema.schema_id, _PROFILE_ID, locator, digest, content["recordCount"])
