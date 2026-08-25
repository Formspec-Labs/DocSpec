from __future__ import annotations

import json
from pathlib import Path

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import LocalJsonlRecordStorage
from docspec.application.planner import RunPlanner, logical_partition
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, sha256_digest, stable_urn
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import DataUsePolicy, RetentionPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.references import LayerRef, SourceCatalogRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from tests.helpers import EMPTY_DIGEST, profile_set
from tests.test_planner import EmptyDocumentCatalog, MemoryControls, MemorySourceCatalog, MemoryStores


def _source_items_in_distinct_partitions(count: int, bucket_count: int) -> tuple[SourceItem, ...]:
    items: list[SourceItem] = []
    partitions: set[int] = set()
    candidate_number = 0
    while len(items) < count:
        item_id = f"item-{candidate_number:08d}"
        candidate_number += 1
        partition = logical_partition(item_id, bucket_count)
        if partition in partitions:
            continue
        partitions.add(partition)
        items.append(
            SourceItem(
                item_id,
                "v1",
                (CandidateFile("primary", f"memory://{item_id}", "text/plain", expected_size=1),),
                metadata={"expectedSegments": 1},
            )
        )
    return tuple(items)


def _planning_plan(
    source_ref: SourceCatalogRef,
    *,
    bucket_count: int,
    max_entries: int,
) -> ProcessingPlan:
    return ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=profile_set(),
        limits=WorkLimits(max_entries, 100, 10, 10, 100, 100, 60),
        stages=StagePolicy(("text-v1",), "paragraph-v1"),
        processors=ProcessorSet(()),
        partition_count=bucket_count,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=EMPTY_DIGEST,
        accepted_failure_policy_digest=EMPTY_DIGEST,
    )


def test_planner_spools_many_touched_partitions_with_deterministic_store_order(tmp_path: Path) -> None:
    bucket_count = 4096
    items = _source_items_in_distinct_partitions(512, bucket_count)
    source_ref = SourceCatalogRef("urn:docspec:test:catalog", "memory://catalog", EMPTY_DIGEST)
    controls = MemoryControls()
    stores = MemoryStores()
    plan = _planning_plan(source_ref, bucket_count=bucket_count, max_entries=2)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    workspace_root = tmp_path / "planning"
    planner = RunPlanner(
        source_catalog=MemorySourceCatalog(source_ref, items),
        document_catalog=EmptyDocumentCatalog(),
        stores=stores,
        controls=controls,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
            workspace_root,
            max_spooled_bytes=16 * 1024**2,
            max_record_bytes=64 * 1024,
            cache_kib=64,
            read_batch_size=1,
        ),
    )

    first = tuple(planner.plan_run(source_ref, None, plan_ref))
    second = tuple(planner.plan_run(source_ref, None, plan_ref))

    assert first == second
    assert len(first) == len(items)
    logical_names = [stores.load(reference).logical_partition for reference in first]
    assert logical_names == sorted(logical_names)
    assert list(workspace_root.glob("*.sqlite3")) == []


def test_planner_preserves_full_then_remaining_store_emission_order(tmp_path: Path) -> None:
    bucket_count = 8
    items = tuple(
        SourceItem(
            f"item-{index:08d}",
            "v1",
            (CandidateFile("primary", f"memory://item-{index:08d}", "text/plain", expected_size=1),),
            metadata={"expectedSegments": 1},
        )
        for index in range(40)
    )
    source_ref = SourceCatalogRef("urn:docspec:test:catalog", "memory://catalog", EMPTY_DIGEST)
    controls = MemoryControls()
    stores = MemoryStores()
    plan = _planning_plan(source_ref, bucket_count=bucket_count, max_entries=1)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    planner = RunPlanner(
        source_catalog=MemorySourceCatalog(source_ref, items),
        document_catalog=EmptyDocumentCatalog(),
        stores=stores,
        controls=controls,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
            tmp_path / "planning-order",
            cache_kib=64,
            read_batch_size=1,
        ),
    )

    last_by_partition: dict[int, str] = {}
    expected: list[str] = []
    for item in items:
        partition = logical_partition(item.item_id, bucket_count)
        if partition in last_by_partition:
            expected.append(last_by_partition[partition])
        last_by_partition[partition] = item.item_id
    expected.extend(last_by_partition[partition] for partition in sorted(last_by_partition))

    references = tuple(planner.plan_run(source_ref, None, plan_ref))
    actual = [stores.load(reference).entries[0].source_item.item_id for reference in references]

    assert actual == expected


def _records_in_distinct_partitions(count: int, bucket_count: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    partitions: set[int] = set()
    candidate_number = 0
    while len(records) < count:
        record_id = f"record-{candidate_number:08d}"
        candidate_number += 1
        partition = partition_bucket(record_id, bucket_count)
        if partition in partitions:
            continue
        partitions.add(partition)
        records.append({"recordId": record_id, "sourceItemId": record_id, "value": len(records)})
    return records


def test_jsonl_writer_bounds_open_members_and_never_whole_reads_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = PartitionPolicy("stress-sha256-v1", 4096)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    records = _records_in_distinct_partitions(512, policy.bucket_count)
    merge_scratch = tmp_path / "merge-scratch"
    merge_scratch.mkdir()
    storage = LocalJsonlRecordStorage(
        tmp_path / "records",
        max_member_bytes=64 * 1024,
        max_open_members=2,
        merge_scratch_root=merge_scratch,
    )
    original_read_bytes = Path.read_bytes
    original_open = Path.open
    input_handles = {"active": 0, "peak": 0}

    class TrackedInput:
        def __init__(self, handle: object) -> None:
            self._handle = handle
            self._closed = False
            input_handles["active"] += 1
            input_handles["peak"] = max(input_handles["peak"], input_handles["active"])

        def __enter__(self) -> TrackedInput:
            return self

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            self.close()

        def close(self) -> None:
            if not self._closed:
                self._closed = True
                self._handle.close()
                input_handles["active"] -= 1

        def __getattr__(self, name: str) -> object:
            return getattr(self._handle, name)

    def track_input_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = original_open(path, mode, *args, **kwargs)
        if mode == "rb" and path.suffix == ".jsonl":
            return TrackedInput(handle)
        return handle

    def reject_whole_member_read(path: Path) -> bytes:
        if path.suffix == ".jsonl" or path.name.startswith("records-"):
            raise AssertionError(f"record member was read whole: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_member_read)
    monkeypatch.setattr(Path, "open", track_input_open)

    first = storage.write_layer(records, layer_kind="stress-records", schema=schema, partition_policy=policy)
    second = storage.write_layer(records, layer_kind="stress-records", schema=schema, partition_policy=policy)

    assert first == second
    assert storage.last_write_peak_open_members == 2
    assert list(storage.stream(second)) == records
    assert storage.last_read_peak_open_members == 2
    assert input_handles == {"active": 0, "peak": 2}
    root = json.loads((storage.root / second.state_ref).read_text())
    assert len(root["members"]) == len(records)
    assert list(storage._staging.iterdir()) == []
    assert list(merge_scratch.iterdir()) == []


def test_jsonl_writer_shards_a_hot_partition_and_reuses_immutable_members(tmp_path: Path) -> None:
    policy = PartitionPolicy("single-partition-v1", 1)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    records = [
        {"recordId": f"record-{index:04d}", "sourceItemId": "hot", "value": index}
        for index in range(24)
    ]
    storage = LocalJsonlRecordStorage(
        tmp_path / "hot-records",
        max_member_bytes=256,
        max_record_bytes=256,
        max_open_members=2,
    )

    first = storage.write_layer(records, layer_kind="hot-records", schema=schema, partition_policy=policy)
    second = storage.write_layer(records, layer_kind="hot-records", schema=schema, partition_policy=policy)
    root = json.loads((storage.root / first.state_ref).read_text())

    assert first == second
    assert len(root["members"]) > 1
    assert [member["sequence"] for member in root["members"]] == list(range(len(root["members"])))
    assert all(member["partition"] == 0 for member in root["members"])
    assert all(member["byteSize"] <= storage.max_member_bytes for member in root["members"])
    assert list(storage.stream(first)) == records


def test_bounded_merge_rejects_cross_run_duplicates_and_cleans_scratch(tmp_path: Path) -> None:
    policy = PartitionPolicy("duplicate-stress-v1", 8)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    scratch = tmp_path / "duplicate-scratch"
    scratch.mkdir()
    storage = LocalJsonlRecordStorage(
        tmp_path / "duplicate-records",
        max_open_members=2,
        merge_scratch_root=scratch,
    )
    values_by_partition: dict[int, str] = {}
    candidate = 0
    while len(values_by_partition) < 3:
        value = f"partition-value-{candidate}"
        candidate += 1
        values_by_partition.setdefault(partition_bucket(value, policy.bucket_count), value)
    partitions = sorted(values_by_partition)[:3]
    identities = ("duplicate", "middle", "duplicate")
    members: list[dict[str, object]] = []
    for partition, identity in zip(partitions, identities, strict=True):
        record = {
            "recordId": identity,
            "sourceItemId": values_by_partition[partition],
            "value": partition,
        }
        payload = canonical_json_bytes(record) + b"\n"
        digest = sha256_digest(payload)
        locator = storage._member_locator(digest)
        path = storage.root / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        members.append(
            {
                "partition": partition,
                "sequence": 0,
                "path": locator,
                "mediaType": "application/x-ndjson",
                "byteSize": len(payload),
                "digest": digest,
                "recordCount": 1,
                "schemaId": schema.schema_id,
            }
        )

    with pytest.raises(IntegrityError, match="globally unique"):
        list(storage._stream_selected_members(members, schema, policy))
    assert list(scratch.iterdir()) == []


def test_record_root_profile_covers_every_supported_occupied_partition(tmp_path: Path) -> None:
    profile = json.loads(
        (Path(__file__).parents[1] / "profiles" / "local-jsonl-records-v1.json").read_text()
    )
    digest = "sha256:" + "a" * 64
    members = [
        {
            "partition": partition,
            "sequence": 0,
            "path": f"record-members/sha256/aa/{'a' * 64}.jsonl",
            "mediaType": "application/x-ndjson",
            "byteSize": 2,
            "digest": digest,
            "recordCount": 1,
            "schemaId": "docspec-test-record/1.0",
        }
        for partition in range(65_536)
    ]
    content = {
        "layerKind": "boundary-records",
        "schema": {
            "schemaId": "docspec-test-record/1.0",
            "fields": ["recordId", "sourceItemId", "value"],
            "identityField": "recordId",
            "partitionField": "sourceItemId",
        },
        "profileId": profile["profileId"],
        "partitionPolicy": {"policyId": "all-supported-partitions-v1", "bucketCount": 65_536},
        "members": members,
        "recordCount": 65_536,
    }
    root = {
        "format": "docspec-record-layer",
        "formatVersion": "1.1",
        "layerId": stable_urn("record-layer", content),
        **content,
    }
    payload = canonical_json_file_bytes(root)
    reference = LayerRef(
        root["layerId"],
        root["layerKind"],
        root["schema"]["schemaId"],
        root["profileId"],
        "boundary.json",
        sha256_digest(payload),
        root["recordCount"],
    )
    record_root = tmp_path / "root-boundary"
    record_root.mkdir()
    (record_root / reference.state_ref).write_bytes(payload)

    assert 16 * 1024**2 < len(payload) <= profile["limits"]["maxRootBytes"]
    undersized = LocalJsonlRecordStorage(record_root, max_root_bytes=len(payload) - 1)
    with pytest.raises(LimitExceededError, match="root exceeds"):
        undersized._load_root(reference)
    exact = LocalJsonlRecordStorage(record_root, max_root_bytes=len(payload))
    assert exact._load_root(reference)["recordCount"] == 65_536
