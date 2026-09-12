"""Shared document catalog fixtures, extracted from tests.conformance.test_document_catalog_contract."""

from __future__ import annotations

from tests.helpers import EMPTY_DIGEST

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from docspec.adapters.storage import LocalDocumentStoreRepository, LocalJsonControlRepository
from docspec.application.commit import ReleaseCommitService
from docspec.domain.content import SourceItem, SourceItemState
from docspec.domain.delivery import core_delivery_schemas
from docspec.domain.identity import (
    ordered_json_sequence_digest,
    sha256_digest,
)
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, StoreVerdict
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileRole
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef, StoreRef
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.ports.document_catalog import DocumentCatalog
from docspec.ports.record_storage import RecordStorage
from docspec.profile_registry import ProfileRegistry, RegisteredProfile
from tests import helpers as _helpers
from tests.support import records as _record_contract

ROOT = Path(__file__).resolve().parents[2]


local_profile_set = _helpers.local_profile_set

persist_execution_evidence = _helpers.persist_execution_evidence

document_release_producer = _helpers.document_release_producer

LAYER_KIND = "conformance-release-records"

SCHEMA = RecordSchema(
    "docspec-conformance-record/1.0",
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


@dataclass(frozen=True)
class _Platform:
    records: RecordStorage
    stores: LocalDocumentStoreRepository
    controls: LocalJsonControlRepository
    catalog: DocumentCatalog


@dataclass(frozen=True)
class _SavedRun:
    reference: DocumentReleaseRef
    plan_ref: ArtifactRef
    run_ref: ArtifactRef
    planned_store: StoreRef
    sealed_store: StoreRef
    artifact: ArtifactRef


def _local_manifest_catalog(registered: RegisteredProfile, platform_root: Path, records, stores, controls):
    module_name, _, attribute = registered.implementation_module.partition(":")
    implementation = getattr(importlib.import_module(module_name), attribute)
    return implementation(
        platform_root / "catalog",
        records=records,
        stores=stores,
        controls=controls,
        producer=document_release_producer(),
        max_release_bytes=registered.description.limits["maxManifestBytes"],
    )


# Each registered catalog profile names the one construction the machine
# description cannot carry; a newly registered profile fails the coverage
# check until it joins this table and passes the same release fixture.
_FACTORIES: dict[str, Callable[..., DocumentCatalog]] = {
    "docspec.document-catalog.local-manifest.v1": _local_manifest_catalog,
}


def _registered_catalog_profiles() -> tuple[RegisteredProfile, ...]:
    profiles = ProfileRegistry.from_directory(ROOT / "src" / "docspec" / "storage_profiles").list(ProfileRole.DOCUMENT_CATALOG)
    assert profiles
    assert {item.description.implementation_id for item in profiles} == set(_FACTORIES), (
        "a registered catalog profile has no conformance factory"
    )
    return profiles


def _platform(registered: RegisteredProfile, root: Path) -> _Platform:
    record_profile = _record_contract._registered_record_profiles()[0]
    records = _record_contract._FACTORIES[record_profile.description.implementation_id](record_profile, root)
    stores = LocalDocumentStoreRepository(root / "stores", verification_scratch=root / "verification-scratch")
    controls = LocalJsonControlRepository(root / "controls")
    catalog = _FACTORIES[registered.description.implementation_id](registered, root, records, stores, controls)
    return _Platform(records, stores, controls, catalog)


def _save_run(
    platform: _Platform,
    *,
    run_tag: str,
    rows: tuple[dict[str, Any], ...],
    base: DocumentReleaseRef | None,
    select_current: bool = True,
) -> _SavedRun:
    """Retain or select one complete run whose logical content is the shared
    extension-layer rows; every release fixture flows through this builder."""

    records = platform.records
    stores = platform.stores
    controls = platform.controls
    active_layers = tuple(
        sorted(
            (
                records.write_layer(rows, layer_kind=LAYER_KIND, schema=SCHEMA, partition_policy=POLICY),
                *(
                    records.write_layer((), layer_kind=kind, schema=schema, partition_policy=POLICY)
                    for kind, schema in core_delivery_schemas().items()
                ),
            ),
            key=lambda item: item.layer_kind,
        )
    )
    stages = StagePolicy(extractor_id="text-v1", extractor_configuration_digest=EMPTY_DIGEST, segmenter_id="paragraph-v1", segmenter_policy_digest=EMPTY_DIGEST, processor_ids=())
    source_digest = sha256_digest(run_tag.encode())
    source = SourceCatalogRef(
        f"urn:docspec:test:catalog:{source_digest.removeprefix('sha256:')}",
        f"external/catalog-{run_tag}.json",
        source_digest,
    )
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    plan = ProcessingPlan.create(
        source_catalog=source,
        base_release=base,
        profiles=local_profile_set(),
        limits=WorkLimits(10, 10_000, 100, 100, 100, 10_000, 60, 3),
        stages=stages,
        processors=ProcessorSet(()),
        partition_count=POLICY.bucket_count,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    deleted = SourceItem(f"source-{run_tag}", "v2", (), SourceItemState.DELETED)
    entry = DocumentEntry.create(deleted, ChangeKind.DELETED, stages)
    planned_job = DocumentStore.planned(
        plan_id=plan.plan_id,
        logical_partition="000",
        entries=(entry,),
        limits=plan.limits,
    )
    planned_job_ref = stores.save(planned_job)
    planned_store_ledger = stores.seal_planned_stores(plan.plan_id, (planned_job_ref,))
    job = planned_job.start(f"attempt-{run_tag}")
    delivery = controls.put(kind="delivery", artifact_id=f"delivery-{run_tag}", value={"complete": True})
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
    execution_profile, execution_handoff, task_result_ledger = persist_execution_evidence(
        controls=controls,
        records=records,
        plan_ref=plan_ref,
        planned_store_ledger=planned_store_ledger,
        planned_stores=(planned_job_ref,),
        sealed_stores=(job_ref,),
        partition_policy=POLICY,
        base_release=base,
    )
    run = RunReceipt.create(
        plan=plan_ref,
        execution_profile=execution_profile,
        execution_handoff=execution_handoff,
        source_catalog=source,
        base_release=base,
        planned_store_ledger=planned_store_ledger,
        store_ledger=store_ledger,
        store_count=1,
        selection_ledger=selection_ledger,
        selected_item_count=1,
        task_result_ledger=task_result_ledger,
        store_receipt_set_digest=ordered_json_sequence_digest((job_ref.to_dict(),)),
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
    staged: list[ArtifactRef] = []
    original_stage = platform.catalog.stage

    def capture_stage(release) -> ArtifactRef:
        reference = original_stage(release)
        staged.append(reference)
        return reference

    platform.catalog.stage = capture_stage  # type: ignore[method-assign]
    try:
        service = ReleaseCommitService(
            plan_ref=plan_ref,
            controls=controls,
            records=records,
            document_catalog=platform.catalog,
        )
        save = service.commit_release if select_current else service.retain_release
        reference = save(base, run_ref)
    finally:
        platform.catalog.stage = original_stage  # type: ignore[method-assign]
    platform.catalog.open(reference)
    assert len(staged) == 1
    return _SavedRun(reference, plan_ref, run_ref, planned_job_ref, job_ref, staged[0])


BASE_ROWS = (
    {"recordId": "alpha", "sourceItemId": "source-alpha", "value": 1},
    {"recordId": "bravo", "sourceItemId": "source-bravo", "value": 2},
)
