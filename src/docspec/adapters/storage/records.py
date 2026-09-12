"""Local records: bounded JSONL partition writing and reading."""

from __future__ import annotations

import heapq
import os
import tempfile
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from docspec.adapters.storage.files import (
    _contained,
    _iter_canonical_json_lines,
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

_PROFILE_ID = "urn:docspec:profile:record-storage:local-jsonl:1"


class _BoundedPartitionWriters:
    """Append to many partition shards while holding a fixed number of handles."""

    def __init__(self, directory: Path, *, max_open: int) -> None:
        if max_open <= 0:
            raise ValueError("max_open must be positive")
        self._directory = directory
        self._max_open = max_open
        self._handles: OrderedDict[tuple[int, int], BinaryIO] = OrderedDict()
        self.paths: dict[tuple[int, int], Path] = {}
        self.peak_open = 0

    def _close_oldest(self) -> None:
        _, handle = self._handles.popitem(last=False)
        handle.close()

    def _open(self, key: tuple[int, int]) -> BinaryIO:
        handle = self._handles.pop(key, None)
        if handle is not None:
            self._handles[key] = handle
            return handle
        if len(self._handles) >= self._max_open:
            self._close_oldest()
        partition, sequence = key
        path = self.paths.get(key)
        if path is None:
            descriptor, name = tempfile.mkstemp(
                prefix=f"records-{partition:05d}-{sequence:05d}-",
                dir=self._directory,
            )
            path = Path(name)
            self.paths[key] = path
            mode = "wb"
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            mode = "ab"
        try:
            handle = os.fdopen(descriptor, mode)
        except BaseException:
            os.close(descriptor)
            raise
        self._handles[key] = handle
        self.peak_open = max(self.peak_open, len(self._handles))
        return handle

    def write(self, key: tuple[int, int], payload: bytes) -> None:
        self._open(key).write(payload)

    def close(self) -> None:
        while self._handles:
            self._close_oldest()


class LocalJsonlRecordStorage:
    """Immutable partitioned JSON Lines layers with reusable partition members."""

    def __init__(
        self,
        root: Path,
        *,
        max_member_bytes: int = 256 * 1024**2,
        max_record_bytes: int = 8 * 1024**2,
        max_root_bytes: int = 32 * 1024**2,
        max_open_members: int = 32,
        max_merge_scratch_bytes: int = 128 * 1024**3,
        merge_scratch_root: Path | None = None,
        create: bool = True,
    ) -> None:
        if min(
            max_member_bytes,
            max_record_bytes,
            max_root_bytes,
            max_open_members,
            max_merge_scratch_bytes,
        ) <= 0:
            raise ValueError("record storage limits must be positive")
        self.root = _storage_root(root, create=create)
        self.max_member_bytes = max_member_bytes
        self.max_record_bytes = max_record_bytes
        self.max_root_bytes = max_root_bytes
        self.max_open_members = max_open_members
        self.max_merge_scratch_bytes = max_merge_scratch_bytes
        self.merge_scratch_root = (
            None if merge_scratch_root is None else _storage_root(merge_scratch_root, create=create)
        )
        self.last_write_peak_open_members = 0
        self.last_read_peak_open_members = 0
        self._staging = _contained(self.root, ".staging/records", create_parents=create).parent
        if create:
            self._staging.mkdir(exist_ok=True)

    @staticmethod
    def _bucket(value: str, count: int) -> int:
        return partition_bucket(value, count)

    @staticmethod
    def _member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest).removeprefix("sha256:")
        return f"record-members/sha256/{hexadecimal[:2]}/{hexadecimal}.jsonl"

    def _load_root(self, reference: LayerRef) -> dict[str, Any]:
        path = _contained(self.root, reference.state_ref)
        if path.is_file() and path.stat().st_size > self.max_root_bytes:
            raise LimitExceededError(f"record layer root exceeds the {self.max_root_bytes}-byte limit")
        payload = _read_exact(self.root, reference.state_ref)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("record layer root differs from its reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.layer_id))
        if not isinstance(value, dict):
            raise IntegrityError("record layer root must be a JSON object")
        return value

    def write_layer(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        layer_kind: str,
        schema: RecordSchema,
        partition_policy: PartitionPolicy,
        base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef:
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
            self.verify(base)
            base_root = self._load_root(base)
            if (
                base_root["layerKind"] != layer_kind
                or base_root["schema"] != self._schema_dict(schema)
                or base_root["partitionPolicy"] != self._policy_dict(partition_policy)
            ):
                raise IntegrityError("incremental layer is incompatible with its base")
            for member in base_root["members"]:
                partition = member["partition"]
                if partition not in replace_partitions:
                    sequence = member.get("sequence", 0)
                    members[(partition, sequence)] = {**member, "sequence": sequence}

        writers = _BoundedPartitionWriters(self._staging, max_open=self.max_open_members)
        counts: dict[tuple[int, int], int] = {}
        sizes: dict[tuple[int, int], int] = {}
        current_sequences: dict[int, int] = {}
        previous_identity: str | None = None
        try:
            for record in records:
                if set(record) != set(schema.fields):
                    raise IntegrityError("record does not match its closed logical schema")
                identity = require_text(record[schema.identity_field], schema.identity_field)
                partition_value = require_text(record[schema.partition_field], schema.partition_field)
                if previous_identity is not None and identity <= previous_identity:
                    raise IntegrityError("record input must be strictly ordered by logical identity")
                previous_identity = identity
                partition = self._bucket(partition_value, partition_policy.bucket_count)
                if replace_partitions is not None and partition not in replace_partitions:
                    raise IntegrityError("incremental records include a partition not declared for replacement")
                line = canonical_json_bytes(record) + b"\n"
                if len(line) > self.max_record_bytes:
                    raise LimitExceededError(f"record exceeds the {self.max_record_bytes}-byte limit")
                if len(line) > self.max_member_bytes:
                    raise LimitExceededError(
                        f"record exceeds the {self.max_member_bytes}-byte member limit"
                    )
                sequence = current_sequences.get(partition, 0)
                key = (partition, sequence)
                if sizes.get(key, 0) + len(line) > self.max_member_bytes:
                    sequence += 1
                    current_sequences[partition] = sequence
                    key = (partition, sequence)
                size = sizes.get(key, 0) + len(line)
                writers.write(key, line)
                counts[key] = counts.get(key, 0) + 1
                sizes[key] = size
            writers.close()
            for key, temporary in writers.paths.items():
                partition, sequence = key
                _sync_file(temporary)
                digest, byte_size = sha256_file(temporary)
                if byte_size != sizes[key]:
                    raise IntegrityError("record member size differs from its streamed write count")
                locator = self._member_locator(digest)
                destination = _contained(self.root, locator, create_parents=True)
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if destination.is_symlink() or not destination.is_file():
                        raise IntegrityError("record member conflicts with an existing immutable object") from None
                    existing_digest, existing_size = sha256_file(destination)
                    if existing_digest != digest or existing_size != byte_size:
                        raise IntegrityError("record member conflicts with an existing immutable object") from None
                members[key] = {
                    "partition": partition,
                    "sequence": sequence,
                    "path": locator,
                    "mediaType": "application/x-ndjson",
                    "byteSize": byte_size,
                    "digest": digest,
                    "recordCount": counts[key],
                    "schemaId": schema.schema_id,
                }
            ordered_members = [members[key] for key in sorted(members)]
            content = {
                "layerKind": layer_kind,
                "schema": self._schema_dict(schema),
                "profileId": _PROFILE_ID,
                "partitionPolicy": self._policy_dict(partition_policy),
                "members": ordered_members,
                "recordCount": sum(member["recordCount"] for member in ordered_members),
            }
            layer_id = stable_urn("record-layer", content)
            root = {
                "format": "docspec-record-layer",
                "formatVersion": "1.1",
                "layerId": layer_id,
                **content,
            }
            root_payload = canonical_json_file_bytes(root)
            if len(root_payload) > self.max_root_bytes:
                raise LimitExceededError(f"record layer root exceeds the {self.max_root_bytes}-byte limit")
            root_digest = sha256_digest(root_payload)
            hexadecimal = root_digest.removeprefix("sha256:")
            locator = f"record-layers/sha256/{hexadecimal[:2]}/{hexadecimal}.json"
            _write_once(self.root, locator, root_payload)
            reference = LayerRef(
                layer_id,
                layer_kind,
                schema.schema_id,
                _PROFILE_ID,
                locator,
                root_digest,
                content["recordCount"],
            )
            self.verify(reference)
            return reference
        finally:
            writers.close()
            self.last_write_peak_open_members = writers.peak_open
            for temporary in writers.paths.values():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _schema_dict(schema: RecordSchema) -> dict[str, Any]:
        return {
            "schemaId": schema.schema_id,
            "fields": list(schema.fields),
            "identityField": schema.identity_field,
            "partitionField": schema.partition_field,
        }

    @staticmethod
    def _policy_dict(policy: PartitionPolicy) -> dict[str, Any]:
        return {"policyId": policy.policy_id, "bucketCount": policy.bucket_count}

    @staticmethod
    def _schema_from_root(root: Mapping[str, Any]) -> RecordSchema:
        value = root["schema"]
        if not isinstance(value, dict) or set(value) != {"schemaId", "fields", "identityField", "partitionField"}:
            raise IntegrityError("record layer schema has an invalid closed shape")
        return RecordSchema(value["schemaId"], tuple(value["fields"]), value["identityField"], value["partitionField"])

    @staticmethod
    def _policy_from_root(root: Mapping[str, Any]) -> PartitionPolicy:
        value = root["partitionPolicy"]
        if not isinstance(value, dict) or set(value) != {"policyId", "bucketCount"}:
            raise IntegrityError("record layer partition policy has an invalid closed shape")
        return PartitionPolicy(value["policyId"], value["bucketCount"])

    def _iter_member(self, member: Mapping[str, Any], schema: RecordSchema) -> Iterator[dict[str, Any]]:
        if member["byteSize"] > self.max_member_bytes:
            raise LimitExceededError(f"record member exceeds the {self.max_member_bytes}-byte limit")
        path = _verified_member_path(
            self.root,
            member,
            media_type="application/x-ndjson",
            schema_id=schema.schema_id,
            extra_fields=frozenset(
                {"partition", "sequence"} if "sequence" in member else {"partition"}
            ),
        )
        if member["path"] != self._member_locator(member["digest"]):
            raise IntegrityError("record member bytes or description differ")
        previous: str | None = None
        count = 0
        for value in _iter_canonical_json_lines(
            path,
            label="record member",
            max_line_bytes=self.max_record_bytes,
        ):
            if set(value) != set(schema.fields):
                raise IntegrityError("record member row does not match its closed logical schema")
            identity = require_text(value[schema.identity_field], schema.identity_field)
            if previous is not None and identity <= previous:
                raise IntegrityError("record member identities are not strictly ordered")
            previous = identity
            count += 1
            yield value
        if count != member["recordCount"]:
            raise IntegrityError("record member count differs from its description")

    def _verified_root(self, reference: LayerRef) -> tuple[dict[str, Any], RecordSchema, PartitionPolicy]:
        root = self._load_root(reference)
        expected_root = {
            "format",
            "formatVersion",
            "layerId",
            "layerKind",
            "schema",
            "profileId",
            "partitionPolicy",
            "members",
            "recordCount",
        }
        if (
            set(root) != expected_root
            or root["format"] != "docspec-record-layer"
            or root["formatVersion"] not in {"1.0", "1.1"}
        ):
            raise IntegrityError("record layer root has an unknown format or invalid closed shape")
        schema = self._schema_from_root(root)
        policy = self._policy_from_root(root)
        if root["profileId"] != _PROFILE_ID:
            raise IntegrityError("record layer names an unknown storage profile")
        expected_content = {
            "layerKind": root["layerKind"],
            "schema": root["schema"],
            "profileId": root["profileId"],
            "partitionPolicy": root["partitionPolicy"],
            "members": root["members"],
            "recordCount": root["recordCount"],
        }
        if stable_urn("record-layer", expected_content) != root["layerId"]:
            raise IntegrityError("record layer identity differs from its canonical content")
        if (
            reference.layer_id != root["layerId"]
            or reference.layer_kind != root["layerKind"]
            or reference.schema_id != schema.schema_id
            or reference.profile_id != root["profileId"]
            or reference.record_count != root["recordCount"]
        ):
            raise IntegrityError("record layer root differs from its reference")
        expected_locator = f"record-layers/sha256/{reference.digest[7:9]}/{reference.digest[7:]}.json"
        if reference.state_ref != expected_locator:
            raise IntegrityError("record layer locator differs from its digest")
        if not isinstance(root["members"], list):
            raise IntegrityError("record layer members must be a list")
        member_keys: list[tuple[int, int]] = []
        declared_total = 0
        for member in root["members"]:
            expected_member = {"partition", "path", "mediaType", "byteSize", "digest", "recordCount", "schemaId"}
            if root["formatVersion"] == "1.1":
                expected_member.add("sequence")
            if not isinstance(member, dict) or set(member) != expected_member:
                raise IntegrityError("record layer member has an invalid closed shape")
            partition = member["partition"]
            if not isinstance(partition, int) or isinstance(partition, bool) or not 0 <= partition < policy.bucket_count:
                raise IntegrityError("record member partition is outside its policy")
            sequence = member.get("sequence", 0)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise IntegrityError("record member sequence is invalid")
            if any(not isinstance(member[name], int) or isinstance(member[name], bool) or member[name] < 0 for name in ("byteSize", "recordCount")):
                raise IntegrityError("record member counts must be non-negative integers")
            require_sha256(member["digest"], "record member digest")
            require_relative_path(member["path"], "record member path")
            if member["path"] != self._member_locator(member["digest"]):
                raise IntegrityError("record member locator differs from its digest")
            declared_total += member["recordCount"]
            member_keys.append((partition, sequence))
        if member_keys != sorted(set(member_keys)):
            raise IntegrityError("record layer partition shards must be distinct and ordered")
        sequences_by_partition: dict[int, list[int]] = {}
        for partition, sequence in member_keys:
            sequences_by_partition.setdefault(partition, []).append(sequence)
        if any(sequences != list(range(len(sequences))) for sequences in sequences_by_partition.values()):
            raise IntegrityError("record layer partition shard sequences must be contiguous")
        if declared_total != root["recordCount"] or declared_total != reference.record_count:
            raise IntegrityError("record layer declared count differs from its members")
        return root, schema, policy

    def _iter_partition_member(
        self,
        member: Mapping[str, Any],
        schema: RecordSchema,
        policy: PartitionPolicy,
    ) -> Iterator[dict[str, Any]]:
        for record in self._iter_member(member, schema):
            if self._bucket(record[schema.partition_field], policy.bucket_count) != member["partition"]:
                raise IntegrityError("logical record appears in the wrong partition")
            yield record

    def _merge_sorted_streams(
        self,
        streams: list[Iterator[dict[str, Any]]],
        schema: RecordSchema,
    ) -> Iterator[dict[str, Any]]:
        """Merge sorted streams with a bounded fan-in and deterministic cleanup."""

        self.last_read_peak_open_members = max(self.last_read_peak_open_members, len(streams))
        heap: list[tuple[str, int, dict[str, Any]]] = []
        previous_identity: str | None = None
        try:
            for index, stream in enumerate(streams):
                record = next(stream, None)
                if record is not None:
                    heapq.heappush(heap, (record[schema.identity_field], index, record))
            while heap:
                identity, index, record = heapq.heappop(heap)
                if previous_identity is not None and identity <= previous_identity:
                    raise IntegrityError("logical record identities are not globally unique and ordered")
                previous_identity = identity
                yield record
                following = next(streams[index], None)
                if following is not None:
                    heapq.heappush(heap, (following[schema.identity_field], index, following))
        finally:
            for stream in streams:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

    def _iter_merge_run(self, path: Path, schema: RecordSchema) -> Iterator[dict[str, Any]]:
        previous_identity: str | None = None
        for record in _iter_canonical_json_lines(
            path,
            label="record merge run",
            max_line_bytes=self.max_record_bytes,
        ):
            if set(record) != set(schema.fields):
                raise IntegrityError("record merge run row does not match its closed logical schema")
            identity = require_text(record[schema.identity_field], schema.identity_field)
            if previous_identity is not None and identity <= previous_identity:
                raise IntegrityError("record merge run identities are not strictly ordered")
            previous_identity = identity
            yield record

    def _write_merge_run(
        self,
        path: Path,
        streams: list[Iterator[dict[str, Any]]],
        schema: RecordSchema,
    ) -> None:
        with path.open("xb") as handle:
            for record in self._merge_sorted_streams(streams, schema):
                handle.write(canonical_json_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _groups[T](values: list[T], size: int) -> Iterator[list[T]]:
        for offset in range(0, len(values), size):
            yield values[offset : offset + size]

    def _stream_selected_members(
        self,
        selected_members: list[Mapping[str, Any]],
        schema: RecordSchema,
        policy: PartitionPolicy,
    ) -> Iterator[dict[str, Any]]:
        """Externally merge partition shards without opening one file per partition."""

        self.last_read_peak_open_members = 0
        if not selected_members:
            return
        if len(selected_members) <= self.max_open_members:
            streams = [self._iter_partition_member(member, schema, policy) for member in selected_members]
            yield from self._merge_sorted_streams(streams, schema)
            return
        if self.max_open_members < 2:
            raise LimitExceededError("record merge fan-in must be at least two for a multi-member layer")

        selected_bytes = sum(member["byteSize"] for member in selected_members)
        if selected_bytes > self.max_merge_scratch_bytes // 2:
            raise LimitExceededError(
                "record merge requires more than the configured bounded scratch capacity"
            )

        with tempfile.TemporaryDirectory(
            prefix="docspec-record-merge-",
            dir=self.merge_scratch_root,
        ) as directory:
            scratch = Path(directory)
            runs: list[Path] = []
            for index, group in enumerate(self._groups(selected_members, self.max_open_members)):
                run = scratch / f"pass-000-{index:08d}.jsonl"
                streams = [self._iter_partition_member(member, schema, policy) for member in group]
                self._write_merge_run(run, streams, schema)
                runs.append(run)

            pass_number = 1
            while len(runs) > self.max_open_members:
                merged_runs: list[Path] = []
                for index, group in enumerate(self._groups(runs, self.max_open_members)):
                    run = scratch / f"pass-{pass_number:03d}-{index:08d}.jsonl"
                    streams = [self._iter_merge_run(source, schema) for source in group]
                    self._write_merge_run(run, streams, schema)
                    for source in group:
                        source.unlink()
                    merged_runs.append(run)
                runs = merged_runs
                pass_number += 1

            yield from self._merge_sorted_streams(
                [self._iter_merge_run(run, schema) for run in runs],
                schema,
            )

    def verify(self, reference: LayerRef) -> None:
        total = sum(1 for _ in self.stream(reference))
        if total != reference.record_count:
            raise IntegrityError("record layer count differs from its members")

    def stream(
        self,
        reference: LayerRef,
        *,
        partitions: frozenset[int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        root, schema, policy = self._verified_root(reference)
        if partitions is not None and any(partition < 0 or partition >= policy.bucket_count for partition in partitions):
            raise ValueError("selected partition is outside the layer partition policy")
        selected_members = [
            member for member in root["members"] if partitions is None or member["partition"] in partitions
        ]
        yield from self._stream_selected_members(selected_members, schema, policy)

    def lookup(
        self,
        reference: LayerRef,
        record_id: str,
        *,
        partition_value: str | None = None,
    ) -> dict[str, Any] | None:
        require_text(record_id, "record_id")
        root, schema, policy = self._verified_root(reference)
        members = root["members"]
        if partition_value is not None:
            require_text(partition_value, "partition_value")
            partition = self._bucket(partition_value, policy.bucket_count)
            members = [member for member in members if member["partition"] == partition]
        for record in self._stream_selected_members(members, schema, policy):
            identity = record[schema.identity_field]
            if identity == record_id:
                return record
            if identity > record_id:
                return None
        return None

    def scan_partition_value(
        self,
        reference: LayerRef,
        partition_value: str,
    ) -> Iterator[dict[str, Any]]:
        require_text(partition_value, "partition_value")
        root, schema, policy = self._verified_root(reference)
        partition = self._bucket(partition_value, policy.bucket_count)
        members = [member for member in root["members"] if member["partition"] == partition]
        yield from self._stream_selected_members(members, schema, policy)

    def identity_field(self, reference: LayerRef) -> str:
        _, schema, _ = self._verified_root(reference)
        return schema.identity_field

    def schema(self, reference: LayerRef) -> RecordSchema:
        """Return the verified logical schema independently of physical members."""

        _, schema, _ = self._verified_root(reference)
        return schema

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy:
        _, _, policy = self._verified_root(reference)
        return policy
