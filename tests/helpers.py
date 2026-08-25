"""Small shared constructors for DocSpec contract tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from rulespec_conformance.platform_artifact import (
    ROOT_OBJECT_KEY,
    LocalMemberSource,
    MemberManifestReference,
    SOURCE_CATALOG_ITEM_SCHEMA_ID,
    SourceCatalogSpec,
    build_artifact_root,
    canonical_json_bytes as artifact_json_bytes,
    describe_member,
    sha256_digest as artifact_set_digest,
    source_catalog_item_schema_bytes,
)

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
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
from docspec.domain.policies import DataUsePolicy, RetentionPolicy
from docspec.domain.processors import ProcessorPayload, ProcessorRecordRef, ProcessorRequest
from docspec.domain.profiles import ProfilePin, ProfileRole, ProfileSet
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, SourceCatalogRef, StoreRef
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.ports.content_fetcher import FetchStream


EMPTY_DIGEST = sha256_digest(b"")
DATA_USE_POLICY = DataUsePolicy.local_content()
RETENTION_POLICY = RetentionPolicy.retain_all()
TASK_RESULT_SCHEMA = RecordSchema(
    "docspec-store-task-result-record/1.0",
    ("recordId", "sourceItemId", "result"),
    "recordId",
    "sourceItemId",
)
_FIXTURE_SOURCE_ORIGIN = "https://fixtures.docspec.test/"


def shared_source_record(item: SourceItem) -> dict[str, object]:
    """Map one test source item to the real shared source-catalog shape."""

    disposition = {
        SourceItemState.ACTIVE: "selected",
        SourceItemState.DELETED: "deleted",
        SourceItemState.EXCLUDED: "excluded",
    }[item.state]
    selection: dict[str, object] = {"disposition": disposition}
    if disposition != "selected":
        selection.update({"reasonCode": f"test.{disposition}", "reason": f"Test item is {disposition}."})
    candidates = []
    for candidate in item.candidates:
        locator = candidate.locator
        if not locator.startswith(("http://", "https://")):
            locator = _FIXTURE_SOURCE_ORIGIN + quote(locator, safe="/")
        candidates.append(
            {
                "renditionId": candidate.candidate_id,
                "mediaType": candidate.media_type,
                "locator": locator,
                "expectedSha256": candidate.expected_digest,
                "expectedByteSize": candidate.expected_size,
            }
        )
    return {
        "sourceItemId": item.item_id,
        "documentId": item.item_id,
        "sourceIssuedVersion": item.version,
        "sourceNativeMetadata": item.metadata,
        "normalizedMetadata": (
            {
                "title": item.item_id,
                "agencies": [{"agencyId": "TEST", "agencyName": "DocSpec test fixture"}],
                "documentType": "TestDocument",
                "publicationDate": "2026-08-24",
                "lastUpdatedDate": None,
                "docketIds": [],
                "regulationIdentifierNumbers": [],
                "commentCloseDate": None,
                "language": "en",
                "sourceUrl": f"https://fixtures.docspec.test/source/{quote(item.item_id, safe='')}",
            }
            if item.state is SourceItemState.ACTIVE
            else None
        ),
        "sourceObservedTopics": [],
        "sourceObservations": [],
        "candidateRenditions": candidates,
        "selection": selection,
    }


def write_shared_source_catalog(
    root: Path,
    items: tuple[dict[str, object] | SourceItem, ...],
    *,
    name: str = "catalog",
    requested_digest: str | None = None,
    selected_digest: str | None = None,
) -> SourceCatalogRef:
    """Publish a small exact shared source-catalog distribution for tests."""

    records = tuple(shared_source_record(item) if isinstance(item, SourceItem) else item for item in items)
    distribution = root / name
    (distribution / "records").mkdir(parents=True)
    (distribution / "schemas").mkdir()
    items_path = distribution / "records/source-items.jsonl"
    items_path.write_bytes(b"".join(artifact_json_bytes(item) + b"\n" for item in records))
    schema_path = distribution / "schemas/source-catalog-item-v1.schema.json"
    schema_path.write_bytes(source_catalog_item_schema_bytes())
    source = LocalMemberSource(distribution)
    members = (
        describe_member(
            source,
            object_key="records/source-items.jsonl",
            role="source-items",
            media_type="application/x-ndjson",
            record_count=len(records),
            schema_id=SOURCE_CATALOG_ITEM_SCHEMA_ID,
        ),
        describe_member(
            source,
            object_key="schemas/source-catalog-item-v1.schema.json",
            role="schema",
            media_type="application/schema+json",
            schema_id=SOURCE_CATALOG_ITEM_SCHEMA_ID,
        ),
    )
    manifest, payload = MemberManifestReference.for_members(
        scope_kind="global",
        scope_id="source-items",
        object_key="manifest.json",
        members=members,
    )
    (distribution / "manifest.json").write_bytes(payload)
    identities = sorted({str(item["sourceItemId"]) for item in records})
    selected = sorted(
        str(item["sourceItemId"])
        for item in records
        if item["selection"] == {"disposition": "selected"}
    )
    artifact_root = build_artifact_root(
        spec=SourceCatalogSpec(
            catalog_id="urn:docspec:test:catalog",
            source_system_id="urn:docspec:test:source",
            source_system_version="1",
            selection_policy_id="urn:docspec:test:selection",
            selection_policy_version="1",
            selection_policy_digest="sha256:" + "1" * 64,
            requested_universe_set_digest=requested_digest or artifact_set_digest(identities),
            selected_source_set_digest=selected_digest or artifact_set_digest(selected),
        ),
        inputs=(),
        manifests=(manifest,),
        accounted_input_count=len(records),
    )
    (distribution / ROOT_OBJECT_KEY).write_bytes(artifact_json_bytes(artifact_root))
    return SourceCatalogRef(
        artifact_root["logicalId"],
        f"{name}/{ROOT_OBJECT_KEY}",
        artifact_root["artifactDigest"],
    )


class SharedFixtureContentFetcher:
    """Resolve the shared test HTTPS namespace through an injected local reader."""

    def __init__(self, root: Path) -> None:
        self._local = LocalFileContentFetcher(root)

    def fetch(self, candidate: CandidateFile, **kwargs):  # type: ignore[no-untyped-def]
        parsed = urlsplit(candidate.locator)
        if parsed.scheme != "https" or parsed.netloc != "fixtures.docspec.test":
            raise ValueError("shared fixture candidate is outside the test source namespace")
        local = replace(candidate, locator=unquote(parsed.path.lstrip("/")))
        result = self._local.fetch(local, **kwargs)
        return FetchStream(
            replace(result.metadata, transport_version=candidate.transport_version),
            result.chunks,
            result.close_callback,
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
        DATA_USE_POLICY.allowed_fields,
        description.item_limits,
        description.cache_policy.key_schema_id or "docspec-cache-disabled/1",
        stable_urn(
            "processor-invocation",
            {"processorId": description.processor_id, "segmentId": segment.segment.segment_id},
        ),
    )


def processor_payload(segment) -> ProcessorPayload:
    """Project one segment through the shared local-only test policy."""

    return ProcessorPayload.for_segment(
        segment.segment,
        segment.content,
        DATA_USE_POLICY.allowed_fields,
    )
