"""Small shared constructors for DocSpec contract tests."""

from __future__ import annotations

from collections.abc import Iterable

from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTaskResult,
    iter_store_tasks,
    summarize_store_tasks,
)
from docspec.domain.identity import canonical_json_file_bytes, identity_digest, sha256_digest, stable_urn
from docspec.domain.processors import ProcessorRecordRef, ProcessorRequest
from docspec.domain.profiles import ProfilePin, ProfileRole, ProfileSet
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, StoreRef
from docspec.domain.storage import PartitionPolicy, RecordSchema


EMPTY_DIGEST = sha256_digest(b"")
TASK_RESULT_SCHEMA = RecordSchema(
    "docspec-store-task-result-record/1.0",
    ("recordId", "sourceItemId", "result"),
    "recordId",
    "sourceItemId",
)


def artifact(identifier: str, *, locator: str | None = None) -> ArtifactRef:
    payload = canonical_json_file_bytes({"id": identifier})
    return ArtifactRef(identifier, locator or f"memory://{identifier}", sha256_digest(payload), "application/json", len(payload))


def profile_set() -> ProfileSet:
    pins = tuple(
        sorted(
            (
                ProfilePin(
                    role=role,
                    profile_id=f"urn:docspec:test-profile:{role.value}",
                    version="1.0.0",
                    implementation_id=f"tests.{role.value}.v1",
                    configuration_digest=EMPTY_DIGEST,
                    description_digest=EMPTY_DIGEST,
                    capabilities=("test-fixture",),
                )
                for role in ProfileRole
            ),
            key=lambda item: item.role.value,
        )
    )
    return ProfileSet(pins)


def local_profile_set(*, result_profile_id: str = "urn:docspec:profile:result-delivery:durable-dataset:1") -> ProfileSet:
    identifiers = {
        ProfileRole.RELEASE_MANIFEST: (
            "urn:docspec:profile:release-manifest:canonical-json:1",
            "docspec.release-manifest.canonical-json.v1",
        ),
        ProfileRole.DOCUMENT_CATALOG: (
            "urn:docspec:profile:document-catalog:local-manifest:1",
            "docspec.document-catalog.local-manifest.v1",
        ),
        ProfileRole.RECORD_STORAGE: (
            "urn:docspec:profile:record-storage:local-jsonl:1",
            "docspec.record-storage.local-jsonl.v1",
        ),
        ProfileRole.BLOB_STORAGE: (
            "urn:docspec:profile:blob-storage:local-content-addressed:1",
            "docspec.blob-storage.local-content-addressed.v1",
        ),
        ProfileRole.DOCUMENT_STORE: (
            "urn:docspec:profile:document-store-persistence:local-json:1",
            "docspec.document-store-persistence.local-json.v1",
        ),
        ProfileRole.RESULT_DELIVERY: (result_profile_id, "docspec.result-delivery.durable-dataset.v1"),
    }
    pins = tuple(
        sorted(
            (
                ProfilePin(
                    role,
                    identifiers[role][0],
                    "1.0.0",
                    identifiers[role][1],
                    identity_digest({}),
                    identity_digest({"profile": identifiers[role][0]}),
                    ("local-reference",),
                )
                for role in ProfileRole
            ),
            key=lambda item: item.role.value,
        )
    )
    return ProfileSet(pins)


def persist_execution_evidence(
    *,
    controls,
    records,
    plan_ref: ArtifactRef,
    planned_store_ledger: LayerRef,
    planned_stores: Iterable[StoreRef],
    sealed_stores: Iterable[StoreRef],
    partition_policy: PartitionPolicy,
    base_release: DocumentReleaseRef | None = None,
) -> tuple[ArtifactRef, ArtifactRef, LayerRef]:
    """Build exact small execution evidence shared by catalog-focused tests."""

    worker = controls.put(
        kind="worker-compositions",
        artifact_id="urn:docspec:test:worker-composition",
        value={"implementationId": "tests.worker/v1"},
    )
    scheduler = controls.put(
        kind="scheduler-configurations",
        artifact_id="urn:docspec:test:scheduler-configuration",
        value={"adapterId": "docspec.local-threaded"},
    )
    profile = ExecutionProfile(
        "docspec.local-threaded",
        "1.0.0",
        worker,
        scheduler,
        ExecutionLimits(1, 1, 1, 1024**3, 1024**3, 100, 1, 1, 0, 0),
        2_000_000_000,
    )
    profile_ref = controls.put(
        kind="execution-profiles",
        artifact_id=profile.profile_id,
        value=profile.to_dict(),
    )
    tasks = tuple(
        iter_store_tasks(
            plan_ref.artifact_id,
            EXECUTE_AND_DELIVER_OPERATION_ID,
            planned_stores,
        )
    )
    task_count, task_digest = summarize_store_tasks(tasks)
    sink = controls.put(
        kind="sinks",
        artifact_id="urn:docspec:test:result-sink",
        value={"sinkId": "urn:docspec:test:result-sink"},
    )
    handoff = ExecutionHandoff(
        processing_plan=plan_ref,
        execution_profile=profile_ref,
        worker_composition=worker,
        planned_store_ledger=planned_store_ledger,
        operation_id=EXECUTE_AND_DELIVER_OPERATION_ID,
        expected_task_count=task_count,
        task_set_digest=task_digest,
        result_sink=sink,
        base_release=base_release,
    )
    handoff_ref = controls.put(
        kind="execution-handoffs",
        artifact_id=handoff.handoff_id,
        value=handoff.to_dict(),
    )
    outputs = {reference.store_id: reference for reference in sealed_stores}
    rows = []
    for task in tasks:
        output = outputs[task.input_store.store_id]
        result = StoreTaskResult.succeeded(
            handoff_id=handoff.handoff_id,
            task=task,
            output_store=output,
        )
        rows.append(
            {
                "recordId": task.input_store.store_id,
                "sourceItemId": task.input_store.store_id,
                "result": result.to_dict(),
            }
        )
    result_ledger = records.write_layer(
        rows,
        layer_kind="execution-task-results",
        schema=TASK_RESULT_SCHEMA,
        partition_policy=partition_policy,
    )
    return profile_ref, handoff_ref, result_ledger


def segment_processor_request(processor, segment, *, prerequisites=()) -> ProcessorRequest:
    """Construct one exact segment request for processor-focused tests."""

    description = processor.description
    input_reference = ProcessorRecordRef.for_segment(segment.segment)
    return ProcessorRequest(
        artifact("urn:docspec:test:processor-plan"),
        description.processor_id,
        identity_digest(description.to_dict()),
        segment.segment.source_item_id,
        (input_reference,),
        tuple(prerequisites),
        ("*",),
        description.item_limits,
        description.cache_policy.key_schema_id or "docspec-cache-disabled/1",
        stable_urn(
            "processor-invocation",
            {"processorId": description.processor_id, "segmentId": segment.segment.segment_id},
        ),
    )
