from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

import docspec.adapters.storage as storage_module
from docspec.adapters.storage import (
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.commit import (
    ReleaseCommitService,
    catalog_commit_token_digest,
    complete_release_counts,
)
from docspec.domain.content import SourceItem, SourceItemState
from docspec.domain.delivery import core_delivery_schemas
from docspec.domain.identity import ordered_json_sequence_digest, sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, StoreVerdict
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileRole
from docspec.domain.receipts import CatalogCommitReceipt, RunReceipt
from docspec.domain.release import DocumentRelease
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError, LimitExceededError
from tests.helpers import local_profile_set, persist_execution_evidence


SCHEMA = RecordSchema(
    "docspec-test-record/1.0",
    ("recordId", "sourceItemId", "value"),
    "recordId",
    "sourceItemId",
)
POLICY = PartitionPolicy("source-item-sha256-v1", 8)
STORE_LEDGER_SCHEMA = RecordSchema(
    "docspec-run-store-reference/1.0",
    ("recordId", "sourceItemId", "store"),
    "recordId",
    "sourceItemId",
)
SELECTION_LEDGER_SCHEMA = RecordSchema(
    "docspec-run-selection/1.0",
    ("recordId", "sourceItemId", "storeId", "entryId", "change", "disposition"),
    "recordId",
    "sourceItemId",
)


def _bucket(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") % POLICY.bucket_count


def test_record_layer_streams_stably_and_reuses_untouched_partitions(tmp_path: Path) -> None:
    storage = LocalJsonlRecordStorage(tmp_path / "records", max_member_bytes=10_000)
    initial_records = [
        {"recordId": "a", "sourceItemId": "source-a", "value": 1},
        {"recordId": "b", "sourceItemId": "source-b", "value": 2},
        {"recordId": "c", "sourceItemId": "source-c", "value": 3},
    ]
    initial = storage.write_layer(
        initial_records,
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    changed_partition = _bucket("source-b")
    replacement_records = [
        {"recordId": "b", "sourceItemId": "source-b", "value": 20},
    ]
    if _bucket("source-c") == changed_partition:
        replacement_records.append({"recordId": "c", "sourceItemId": "source-c", "value": 3})
    updated = storage.write_layer(
        replacement_records,
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
        base=initial,
        replace_partitions=frozenset({changed_partition}),
    )

    assert list(storage.stream(updated)) == [
        {"recordId": "a", "sourceItemId": "source-a", "value": 1},
        {"recordId": "b", "sourceItemId": "source-b", "value": 20},
        {"recordId": "c", "sourceItemId": "source-c", "value": 3},
    ]
    assert list(storage.stream(updated, partitions=frozenset({changed_partition}))) == replacement_records
    initial_root = json.loads((storage.root / initial.state_ref).read_text())
    updated_root = json.loads((storage.root / updated.state_ref).read_text())
    initial_paths = {item["partition"]: item["path"] for item in initial_root["members"]}
    updated_paths = {item["partition"]: item["path"] for item in updated_root["members"]}
    for partition in initial_paths.keys() - {changed_partition}:
        assert updated_paths[partition] == initial_paths[partition]


def test_identical_record_layer_roots_publish_atomically_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalJsonlRecordStorage(tmp_path / "records")
    first_writer_entered = Event()
    release_first_writer = Event()
    call_lock = Lock()
    first_call = True
    original_fdopen = storage_module.os.fdopen

    def gated_fdopen(descriptor: int, *args: Any, **kwargs: Any):
        nonlocal first_call
        with call_lock:
            should_wait = first_call
            first_call = False
        if should_wait:
            first_writer_entered.set()
            assert release_first_writer.wait(timeout=5)
        return original_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "fdopen", gated_fdopen)

    def write_empty_layer():
        return storage.write_layer(
            (),
            layer_kind="test-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write_empty_layer)
        assert first_writer_entered.wait(timeout=5)
        second = pool.submit(write_empty_layer)
        try:
            second_reference = second.result(timeout=5)
        finally:
            release_first_writer.set()
        first_reference = first.result(timeout=5)

    assert first_reference == second_reference
    storage.verify(first_reference)


def test_record_layer_rejects_unsorted_open_or_tampered_records(tmp_path: Path) -> None:
    storage = LocalJsonlRecordStorage(tmp_path / "records")
    with pytest.raises(IntegrityError, match="strictly ordered"):
        storage.write_layer(
            [
                {"recordId": "b", "sourceItemId": "source-b", "value": 2},
                {"recordId": "a", "sourceItemId": "source-a", "value": 1},
            ],
            layer_kind="test-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )

    layer = storage.write_layer(
        [{"recordId": "a", "sourceItemId": "source-a", "value": 1}],
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    root = json.loads((storage.root / layer.state_ref).read_text())
    (storage.root / root["members"][0]["path"]).write_bytes(b'{"recordId":"a"}\n')
    with pytest.raises(IntegrityError):
        storage.verify(layer)


def _committed_catalog_state(tmp_path: Path):
    records = LocalJsonlRecordStorage(tmp_path / "records")
    stores = LocalDocumentStoreRepository(
        tmp_path / "stores",
        verification_scratch=tmp_path / "verification-scratch",
    )
    controls = LocalJsonControlRepository(tmp_path / "controls")
    catalog = LocalManifestDocumentCatalog(tmp_path / "catalog", records=records, stores=stores, controls=controls)
    extension_layer = records.write_layer(
        [{"recordId": "a", "sourceItemId": "source-a", "value": 1}],
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    active_layers = tuple(
        sorted(
            (
                extension_layer,
                *(
                    records.write_layer(
                        (),
                        layer_kind=kind,
                        schema=schema,
                        partition_policy=POLICY,
                    )
                    for kind, schema in core_delivery_schemas().items()
                ),
            ),
            key=lambda item: item.layer_kind,
        )
    )
    stages = StagePolicy(("text-v1",), "paragraph-v1")
    source = SourceCatalogRef("catalog-1", "external/catalog.json", sha256_digest(b"catalog-1"))
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    plan = ProcessingPlan.create(
        source_catalog=source,
        base_release=None,
        profiles=local_profile_set(),
        limits=WorkLimits(10, 10_000, 100, 100, 100, 10_000, 60, 3),
        stages=stages,
        processors=ProcessorSet(()),
        partition_count=POLICY.bucket_count,
        selection={},
        retention_policy={"sourceBytes": "retained"},
        data_use_policy={"dataUse": "local-bytes-only"},
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    deleted = SourceItem("source-a", "v2", (), SourceItemState.DELETED)
    entry = DocumentEntry.create(deleted, ChangeKind.DELETED, stages)
    planned_job = DocumentStore.planned(
        plan_id=plan.plan_id,
        logical_partition="000",
        entries=(entry,),
        limits=plan.limits,
    )
    planned_job_ref = stores.save(planned_job)
    planned_store_ledger = stores.seal_planned_stores(plan.plan_id, (planned_job_ref,))
    job = planned_job.start("attempt-1")
    delivery = controls.put(kind="delivery", artifact_id="delivery-1", value={"complete": True})
    job = job.seal(StoreVerdict.COMPLETED, delivery)
    job_ref = stores.save(job)
    store_ledger = records.write_layer(
        ({"recordId": job.store_id, "sourceItemId": job.store_id, "store": job_ref.to_dict()},),
        layer_kind="run-store-receipts",
        schema=STORE_LEDGER_SCHEMA,
        partition_policy=POLICY,
    )
    selection_ledger = records.write_layer(
        (
            {
                "recordId": deleted.item_id,
                "sourceItemId": deleted.item_id,
                "storeId": job.store_id,
                "entryId": entry.entry_id,
                "change": entry.change.value,
                "disposition": entry.disposition.value,
            },
        ),
        layer_kind="run-selection",
        schema=SELECTION_LEDGER_SCHEMA,
        partition_policy=POLICY,
    )
    store_digest = ordered_json_sequence_digest((job_ref.to_dict(),))
    execution_profile, execution_handoff, task_result_ledger = persist_execution_evidence(
        controls=controls,
        records=records,
        plan_ref=plan_ref,
        planned_store_ledger=planned_store_ledger,
        planned_stores=(planned_job_ref,),
        sealed_stores=(job_ref,),
        partition_policy=POLICY,
    )
    run = RunReceipt.create(
        plan=plan_ref,
        execution_profile=execution_profile,
        execution_handoff=execution_handoff,
        source_catalog=source,
        base_release=None,
        planned_store_ledger=planned_store_ledger,
        store_ledger=store_ledger,
        store_count=1,
        selection_ledger=selection_ledger,
        selected_item_count=1,
        task_result_ledger=task_result_ledger,
        store_receipt_set_digest=store_digest,
        staged_layers=active_layers,
        blob_roots=(),
        counts={"stores": 1, "selectedItems": 1, "rejectedStores": 0},
        failures={"counts": {}, "first": None},
        coverage={"complete": True},
        partition_policy={"policyId": POLICY.policy_id, "bucketCount": POLICY.bucket_count},
        stateful=True,
        completed_at="2026-08-05T12:00:00Z",
    )
    run_ref = controls.put(kind="run-receipts", artifact_id=run.run_id, value=run.to_dict())
    reference = ReleaseCommitService(
        plan_ref=plan_ref,
        controls=controls,
        records=records,
        document_catalog=catalog,
    ).commit_release(None, run_ref)
    release = catalog.open(reference)
    return records, stores, controls, catalog, plan, plan_ref, run, run_ref, release, reference


def _release_with(release: DocumentRelease, **changes: Any) -> DocumentRelease:
    values = {
        "previous_release": release.previous_release,
        "source_catalog": release.source_catalog,
        "processing_plan": release.processing_plan,
        "profiles": release.profiles,
        "active_layers": release.active_layers,
        "blob_roots": release.blob_roots,
        "retention_dispositions": release.retention_dispositions,
        "store_receipt_set_digest": release.store_receipt_set_digest,
        "run_receipt": release.run_receipt,
        "catalog_commit_receipt": release.catalog_commit_receipt,
        "counts": release.counts,
        "failures": release.failures,
        "coverage": release.coverage,
        "partition_policy": release.partition_policy,
    }
    values.update(changes)
    return DocumentRelease.create(**values)


def _release_with_blob_roots(
    *,
    controls: LocalJsonControlRepository,
    plan: ProcessingPlan,
    run: RunReceipt,
    release: DocumentRelease,
    blob_roots: tuple[ArtifactRef, ...],
) -> DocumentRelease:
    changed_run = RunReceipt.create(
        plan=run.plan,
        execution_profile=run.execution_profile,
        execution_handoff=run.execution_handoff,
        source_catalog=run.source_catalog,
        base_release=run.base_release,
        planned_store_ledger=run.planned_store_ledger,
        store_ledger=run.store_ledger,
        store_count=run.store_count,
        selection_ledger=run.selection_ledger,
        selected_item_count=run.selected_item_count,
        task_result_ledger=run.task_result_ledger,
        store_receipt_set_digest=run.store_receipt_set_digest,
        staged_layers=run.staged_layers,
        blob_roots=blob_roots,
        counts=run.counts,
        failures=run.failures,
        coverage=run.coverage,
        partition_policy=run.partition_policy,
        stateful=run.stateful,
        completed_at=run.completed_at,
    )
    run_ref = controls.put(kind="run-receipts", artifact_id=changed_run.run_id, value=changed_run.to_dict())
    commit = CatalogCommitReceipt.create(
        profile_id=plan.profiles.for_role(ProfileRole.DOCUMENT_CATALOG).profile_id,
        base_release=release.previous_release,
        expected_head=release.previous_release,
        run_receipt=run_ref,
        commit_token_digest=catalog_commit_token_digest(
            base_release=release.previous_release,
            run_receipt=run_ref,
            store_receipt_set_digest=changed_run.store_receipt_set_digest,
            layers=changed_run.staged_layers,
        ),
        prepared_at=changed_run.completed_at,
    )
    commit_ref = controls.put(
        kind="catalog-commit-receipts",
        artifact_id=commit.receipt_id,
        value=commit.to_dict(),
    )
    return _release_with(
        release,
        run_receipt=run_ref,
        catalog_commit_receipt=commit_ref,
        blob_roots=blob_roots,
        counts=complete_release_counts(release.active_layers, blob_roots),
    )


def test_manifest_catalog_commits_and_reopens_complete_state(tmp_path: Path) -> None:
    records, stores, controls, catalog, _, _, _, _, release, reference = _committed_catalog_state(tmp_path)

    assert catalog.current() == reference
    before = {
        f"{root.name}/{path.relative_to(root).as_posix()}": sha256_digest(path.read_bytes())
        for root in (catalog.root, stores.root)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert catalog.open(reference) == release
    after = {
        f"{root.name}/{path.relative_to(root).as_posix()}": sha256_digest(path.read_bytes())
        for root in (catalog.root, stores.root)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert list((tmp_path / "verification-scratch").iterdir()) == []
    assert catalog.lookup(reference, layer_kind="test-records", record_id="a") == {
        "recordId": "a",
        "sourceItemId": "source-a",
        "value": 1,
    }
    assert list(catalog.scan(reference, layer_kind="test-records"))[0]["recordId"] == "a"

    bounded_catalog = LocalManifestDocumentCatalog(
        tmp_path / "bounded-catalog",
        records=records,
        stores=stores,
        controls=controls,
        max_release_bytes=64,
    )
    with pytest.raises(LimitExceededError, match="document release"):
        bounded_catalog.stage(release)


def test_manifest_catalog_rejects_valid_but_unrelated_release_state(tmp_path: Path) -> None:
    _, _, controls, catalog, plan, _, run, run_ref, release, _ = _committed_catalog_state(tmp_path)
    unrelated_base = DocumentReleaseRef(
        "urn:docspec:document-release:v1:unrelated",
        "document-catalog/releases/unrelated.json",
        sha256_digest(b"unrelated release"),
    )
    unrelated_source = SourceCatalogRef(
        "catalog-unrelated",
        "external/unrelated.json",
        sha256_digest(b"unrelated catalog"),
    )
    unrelated_plan = ProcessingPlan.create(
        source_catalog=plan.source_catalog,
        base_release=plan.base_release,
        profiles=plan.profiles,
        limits=plan.limits,
        stages=plan.stages,
        processors=plan.processors,
        partition_count=plan.partition_count,
        selection={"unrelated": True},
        retention_policy=plan.retention_policy,
        data_use_policy=plan.data_use_policy,
        retry_policy_digest=plan.retry_policy_digest,
        accepted_failure_policy_digest=plan.accepted_failure_policy_digest,
    )
    unrelated_plan_ref = controls.put(
        kind="plans",
        artifact_id=unrelated_plan.plan_id,
        value=unrelated_plan.to_dict(),
    )
    unrelated_run = RunReceipt.create(
        plan=run.plan,
        execution_profile=run.execution_profile,
        execution_handoff=run.execution_handoff,
        source_catalog=run.source_catalog,
        base_release=run.base_release,
        planned_store_ledger=run.planned_store_ledger,
        store_ledger=run.store_ledger,
        store_count=run.store_count,
        selection_ledger=run.selection_ledger,
        selected_item_count=run.selected_item_count,
        task_result_ledger=run.task_result_ledger,
        store_receipt_set_digest=run.store_receipt_set_digest,
        staged_layers=run.staged_layers,
        blob_roots=run.blob_roots,
        counts=run.counts,
        failures=run.failures,
        coverage={"unrelated": True},
        partition_policy=run.partition_policy,
        stateful=run.stateful,
        completed_at=run.completed_at,
    )
    unrelated_run_ref = controls.put(
        kind="run-receipts",
        artifact_id=unrelated_run.run_id,
        value=unrelated_run.to_dict(),
    )
    unrelated_commit = CatalogCommitReceipt.create(
        profile_id=plan.profiles.for_role(ProfileRole.DOCUMENT_CATALOG).profile_id,
        base_release=release.previous_release,
        expected_head=unrelated_base,
        run_receipt=run_ref,
        commit_token_digest=catalog_commit_token_digest(
            base_release=release.previous_release,
            run_receipt=run_ref,
            store_receipt_set_digest=run.store_receipt_set_digest,
            layers=run.staged_layers,
        ),
        prepared_at=run.completed_at,
    )
    unrelated_commit_ref = controls.put(
        kind="catalog-commit-receipts",
        artifact_id=unrelated_commit.receipt_id,
        value=unrelated_commit.to_dict(),
    )
    variants = {
        "source": _release_with(release, source_catalog=unrelated_source),
        "base": _release_with(release, previous_release=unrelated_base),
        "plan": _release_with(release, processing_plan=unrelated_plan_ref),
        "profiles": _release_with(release, profiles=local_profile_set(result_profile_id="unrelated-result")),
        "active layers": _release_with(release, active_layers=()),
        "store digest": _release_with(release, store_receipt_set_digest=sha256_digest(b"unrelated stores")),
        "partition policy": _release_with(
            release,
            partition_policy={"policyId": POLICY.policy_id, "bucketCount": POLICY.bucket_count // 2},
        ),
        "counts": _release_with(release, counts={"unrelated": 1}),
        "coverage": _release_with(release, coverage={"unrelated": True}),
        "run linkage": _release_with(release, run_receipt=unrelated_run_ref),
        "commit linkage": _release_with(release, catalog_commit_receipt=unrelated_commit_ref),
    }
    for label, candidate in variants.items():
        with pytest.raises(IntegrityError) as rejected:
            catalog.stage(candidate)
        assert str(rejected.value), label


def test_manifest_catalog_verifies_every_blob_root_and_its_pinned_profile(tmp_path: Path) -> None:
    _, _, controls, catalog, plan, _, run, _, release, _ = _committed_catalog_state(tmp_path)
    missing = ArtifactRef(
        "urn:docspec:test:missing-blob-root",
        "control/profile-state/missing.json",
        sha256_digest(b"missing"),
        "application/json",
        len(b"missing"),
    )
    wrong_profile = controls.put(
        kind="profile-state",
        artifact_id="urn:docspec:test:wrong-profile-blob-root",
        value={
            "profileId": "urn:docspec:profile:blob-storage:unrelated:1",
            "profileVersion": plan.profiles.for_role(ProfileRole.BLOB_STORAGE).version,
            "storageRoot": "/test/blobs",
        },
    )
    valid_but_unrelated = controls.put(
        kind="profile-state",
        artifact_id="urn:docspec:test:unrelated-blob-root",
        value={
            "profileId": plan.profiles.for_role(ProfileRole.BLOB_STORAGE).profile_id,
            "profileVersion": plan.profiles.for_role(ProfileRole.BLOB_STORAGE).version,
            "storageRoot": "/test/unrelated-blobs",
        },
    )
    wrong_version = controls.put(
        kind="profile-state",
        artifact_id="urn:docspec:test:wrong-version-blob-root",
        value={
            "profileId": plan.profiles.for_role(ProfileRole.BLOB_STORAGE).profile_id,
            "profileVersion": "0.0.0",
            "storageRoot": "/test/blobs",
        },
    )
    incomplete = controls.put(
        kind="profile-state",
        artifact_id="urn:docspec:test:incomplete-blob-root",
        value={
            "profileId": plan.profiles.for_role(ProfileRole.BLOB_STORAGE).profile_id,
            "profileVersion": plan.profiles.for_role(ProfileRole.BLOB_STORAGE).version,
        },
    )

    missing_release = _release_with_blob_roots(
        controls=controls,
        plan=plan,
        run=run,
        release=release,
        blob_roots=(missing,),
    )
    mismatched_release = _release_with_blob_roots(
        controls=controls,
        plan=plan,
        run=run,
        release=release,
        blob_roots=(wrong_profile,),
    )
    receipt_mismatch = _release_with(
        release,
        blob_roots=(valid_but_unrelated,),
        counts=complete_release_counts(release.active_layers, (valid_but_unrelated,)),
    )
    wrong_version_release = _release_with_blob_roots(
        controls=controls,
        plan=plan,
        run=run,
        release=release,
        blob_roots=(wrong_version,),
    )
    incomplete_release = _release_with_blob_roots(
        controls=controls,
        plan=plan,
        run=run,
        release=release,
        blob_roots=(incomplete,),
    )

    with pytest.raises(IntegrityError, match="missing"):
        catalog.stage(missing_release)
    with pytest.raises(IntegrityError, match="blob root uses a profile"):
        catalog.stage(mismatched_release)
    with pytest.raises(IntegrityError, match="blob roots differ"):
        catalog.stage(receipt_mismatch)
    with pytest.raises(IntegrityError, match="profile not pinned"):
        catalog.stage(wrong_version_release)
    with pytest.raises(IntegrityError, match="invalid closed shape"):
        catalog.stage(incomplete_release)
