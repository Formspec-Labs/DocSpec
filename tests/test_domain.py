from __future__ import annotations

from dataclasses import fields, replace

import pytest

from docspec.domain.content import AcquisitionDisposition, CandidateFile, SourceItem
from docspec.domain.identity import canonical_json_file_bytes, parse_canonical_json, sha256_digest
from docspec.domain.jobs import (
    ChangeKind,
    DocumentEntry,
    DocumentStore,
    FailureClass,
    FailureRecord,
    StoreState,
    StoreVerdict,
)
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorCachePolicy,
    ProcessorDescription,
    ProcessorInput,
    ProcessorItemLimits,
    ProcessorResourceIdentity,
    ProcessorResourceKind,
    ProcessorSet,
)
from docspec.domain.references import DocumentReleaseRef, SourceCatalogRef
from docspec.domain.release import DocumentRelease
from docspec.errors import IntegrityError, StateTransitionError
from tests.helpers import EMPTY_DIGEST, artifact, profile_set


def _limits() -> WorkLimits:
    return WorkLimits(2, 1000, 20, 100, 100, 1000, 60, 2)


def _item(identifier: str = "item-1") -> SourceItem:
    return SourceItem(identifier, "v1", (CandidateFile("primary", f"memory://{identifier}", "text/plain", expected_size=5),))


def _plan() -> ProcessingPlan:
    source = SourceCatalogRef("catalog-1", "memory://catalog", sha256_digest(b"catalog"))
    return ProcessingPlan.create(
        source_catalog=source,
        base_release=None,
        profiles=profile_set(),
        limits=_limits(),
        stages=StagePolicy(("text-v1",), "paragraph-v1"),
        processors=ProcessorSet(()),
        partition_count=4,
        selection={},
        retention_policy={"sourceBytes": "retain"},
        data_use_policy={"externalProcessing": False},
        retry_policy_digest=EMPTY_DIGEST,
        accepted_failure_policy_digest=EMPTY_DIGEST,
    )


def test_canonical_json_rejects_duplicate_keys_and_noncanonical_bytes() -> None:
    with pytest.raises(IntegrityError, match="duplicate key"):
        parse_canonical_json(b'{"a":1,"a":2}\n')
    with pytest.raises(IntegrityError, match="not canonical"):
        parse_canonical_json(b'{"b":2, "a":1}\n')
    assert parse_canonical_json(canonical_json_file_bytes({"b": 2, "a": 1})) == {"a": 1, "b": 2}


def test_processing_plan_round_trips_all_work_limit_fields() -> None:
    plan = _plan()
    assert ProcessingPlan.from_dict(plan.to_dict()) == plan
    assert ProcessingPlan.from_dict(plan.to_dict()).limits == _limits()


def test_processing_plan_pins_the_complete_processor_graph() -> None:
    source = SourceCatalogRef("catalog-1", "memory://catalog", sha256_digest(b"catalog"))
    description = _processor("stats-v1")

    def create(processor: ProcessorDescription) -> ProcessingPlan:
        return ProcessingPlan.create(
            source_catalog=source,
            base_release=None,
            profiles=profile_set(),
            limits=_limits(),
            stages=StagePolicy(("text-v1",), "paragraph-v1", (processor.processor_id,)),
            processors=ProcessorSet((processor,)),
            partition_count=4,
            selection={},
            retention_policy={"sourceBytes": "retain"},
            data_use_policy={"externalProcessing": False},
            retry_policy_digest=EMPTY_DIGEST,
            accepted_failure_policy_digest=EMPTY_DIGEST,
        )

    original = create(description)
    changed = create(_reidentified(description, implementation_id="tests.stats-v2"))

    assert original.plan_id != changed.plan_id
    assert ProcessingPlan.from_dict(original.to_dict()).processors == ProcessorSet((description,))
    with pytest.raises(ValueError, match="pinned processor graph"):
        replace(original, processors=ProcessorSet(()))


def test_document_store_is_a_bounded_revisioned_job_and_receipt() -> None:
    stages = StagePolicy(("text-v1",), "paragraph-v1")
    entry = DocumentEntry.create(_item(), ChangeKind.ADDED, stages)
    store = DocumentStore.planned(plan_id="plan-1", logical_partition="bucket-00000/store-00000000", entries=(entry,), limits=_limits())
    running = store.start("attempt-1")
    completed_entry = replace(entry, disposition=AcquisitionDisposition.CAPTURED)
    checkpoint = running.checkpoint((completed_entry,))
    receipt = artifact("delivery-1")
    sealed = checkpoint.seal(StoreVerdict.COMPLETED, receipt)

    assert store.state == StoreState.PLANNED
    assert sealed.state == StoreState.SEALED
    assert sealed.revision == 3
    assert DocumentStore.from_dict(sealed.to_dict()) == sealed
    assert sealed.receipt_digest.startswith("sha256:")
    with pytest.raises(StateTransitionError):
        sealed.start("attempt-2")


def test_failure_class_controls_retryability() -> None:
    assert FailureRecord(FailureClass.TRANSIENT_EXTERNAL, "test.timeout", "timeout", 1, True)
    with pytest.raises(ValueError, match="retryability"):
        FailureRecord(FailureClass.DETERMINISTIC_INPUT, "test.bad-input", "bad", 1, True)


def _processor(identifier: str, dependencies: tuple[str, ...] = ()) -> ProcessorDescription:
    return ProcessorDescription.create(
        name=identifier,
        version="1",
        implementation_id=f"tests.{identifier}",
        accepted_inputs=(ProcessorInput("segment", ("docspec-segment/1",), ("text/plain",)),),
        output_schema_id=f"schema:{identifier}",
        output_media_types=("application/json",),
        external_resources=(),
        dependencies=dependencies,
        deterministic=True,
        cache_policy=ProcessorCachePolicy(
            ProcessorCacheMode.EXACT_INPUTS,
            "docspec-exact-processor-cache-key/1",
        ),
        configuration_digest=EMPTY_DIGEST,
        data_use_policy_digest=EMPTY_DIGEST,
        item_limits=ProcessorItemLimits(1, 1024, 1, 1024, 30),
        retry_policy_digest=EMPTY_DIGEST,
    )


def _reidentified(description: ProcessorDescription, **changes: object) -> ProcessorDescription:
    values = {
        "name": description.name,
        "version": description.version,
        "implementation_id": description.implementation_id,
        "accepted_inputs": description.accepted_inputs,
        "output_schema_id": description.output_schema_id,
        "output_media_types": description.output_media_types,
        "external_resources": description.external_resources,
        "dependencies": description.dependencies,
        "deterministic": description.deterministic,
        "cache_policy": description.cache_policy,
        "configuration_digest": description.configuration_digest,
        "data_use_policy_digest": description.data_use_policy_digest,
        "item_limits": description.item_limits,
        "retry_policy_digest": description.retry_policy_digest,
        "capabilities": description.capabilities,
    }
    values.update(changes)
    return ProcessorDescription.create(**values)


def _malformed_processor(identifier: str, dependencies: tuple[str, ...]) -> ProcessorDescription:
    """Bypass record validation solely to exercise ProcessorSet's cycle defense."""

    description = object.__new__(ProcessorDescription)
    baseline = _processor(identifier)
    for field in fields(ProcessorDescription):
        object.__setattr__(description, field.name, getattr(baseline, field.name))
    object.__setattr__(description, "processor_id", identifier)
    object.__setattr__(description, "dependencies", dependencies)
    return description


def test_processor_graph_invalidates_only_transitive_dependents() -> None:
    extract = _processor("extract")
    classify = _processor("classify", (extract.processor_id,))
    summary = _processor("summary")
    graph = ProcessorSet((extract, classify, summary))
    assert graph.invalidated_by((extract.processor_id,)) == (extract.processor_id, classify.processor_id)
    with pytest.raises(ValueError, match="acyclic"):
        ProcessorSet((_malformed_processor("a", ("b",)), _malformed_processor("b", ("a",))))
    with pytest.raises(ValueError, match="unknown dependencies"):
        ProcessorSet((_processor("classify", ("missing",)),))
    with pytest.raises(ValueError, match="names must be distinct"):
        ProcessorSet((_processor("duplicate"), _reidentified(_processor("duplicate"), version="2")))


def test_processor_description_is_closed_provider_neutral_and_identity_bearing() -> None:
    baseline = _processor("fixture")
    model = ProcessorResourceIdentity(
        resource_id="fixture-model",
        resource_kind=ProcessorResourceKind.MODEL,
        revision="2026-08-05",
        identity_digest=sha256_digest(b"fixture-model-identity"),
    )
    variants = (
        _reidentified(
            baseline,
            accepted_inputs=(ProcessorInput("segment", ("docspec-segment/2",), ("text/plain",)),),
        ),
        _reidentified(baseline, output_media_types=("application/vnd.example+json",)),
        _reidentified(baseline, external_resources=(model,)),
        _reidentified(baseline, cache_policy=ProcessorCachePolicy(ProcessorCacheMode.DISABLED, None)),
        _reidentified(baseline, item_limits=replace(baseline.item_limits, max_output_bytes=2048)),
    )

    identities = {ProcessorSet((description,)).processor_set_id for description in (baseline, *variants)}
    assert len(identities) == 1 + len(variants)
    assert ProcessorDescription.from_dict(baseline.to_dict()) == baseline
    assert baseline.input_kinds == ("segment",)

    extra = baseline.to_dict()
    extra["providerClient"] = "vendor-specific"
    with pytest.raises(ValueError, match="closed shape"):
        ProcessorDescription.from_dict(extra)

    tampered = baseline.to_dict()
    tampered["itemLimits"]["maxOutputBytes"] = 2048
    with pytest.raises(ValueError, match="identity differs"):
        ProcessorDescription.from_dict(tampered)

    class ProviderSdkModel:
        pass

    with pytest.raises(ValueError, match="ProcessorResourceIdentity"):
        replace(baseline, external_resources=(ProviderSdkModel(),))
    with pytest.raises(ValueError, match="deterministic"):
        replace(baseline, deterministic=False)


def test_document_release_is_complete_versioned_and_tamper_evident() -> None:
    plan = _plan()
    plan_ref = plan.artifact_ref(locator="memory://plan")
    release = DocumentRelease.create(
        previous_release=None,
        source_catalog=plan.source_catalog,
        processing_plan=plan_ref,
        profiles=plan.profiles,
        active_layers=(),
        blob_roots=(),
        retention_dispositions={"sourceBytes": "retained"},
        store_receipt_set_digest=sha256_digest(b"stores"),
        run_receipt=artifact("run-1"),
        catalog_commit_receipt=artifact("commit-1"),
        counts={"sourceItems": 0},
        failures={},
        coverage={"selected": 0},
        partition_policy={"identity": "sha256-v1", "bucketCount": 4},
    )
    assert DocumentRelease.from_dict(release.to_dict()) == release
    reference = release.reference("memory://release")
    assert DocumentReleaseRef.from_dict(reference.to_dict()) == reference

    changed = release.to_dict()
    changed["counts"] = {"sourceItems": 1}
    with pytest.raises(ValueError, match="identity differs"):
        DocumentRelease.from_dict(changed)
