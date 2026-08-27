from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest

from docspec.domain.profiles import ProfilePin, ProfileRole, ProfileSet
from docspec.domain.scale import (
    ScaleDocumentProcessingWorkload,
    ScaleProfile,
    ScaleResult,
    ScaleSourceCatalogWorkload,
    ScaleWorkloadKind,
)
from docspec.errors import IntegrityError, ProfileError

ZERO_DIGEST = "sha256:" + "0" * 64
PROFILE_LOCATOR = "fixture://scale-profiles/source-catalog.json"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _artifact(identity: str) -> dict[str, str]:
    return {
        "artifactId": identity,
        "locator": f"fixture://artifacts/{identity.rsplit(':', 1)[-1]}.json",
        "digest": ZERO_DIGEST,
    }


def document_processing_workload_content() -> dict[str, object]:
    pins = tuple(
        sorted(
            (
                ProfilePin(
                    role,
                    f"urn:docspec:test:profile:{role.value}",
                    "1.0.0",
                    f"docspec.test.{role.name.casefold()}.v1",
                    ZERO_DIGEST,
                    ZERO_DIGEST,
                    ("bounded",),
                )
                for role in ProfileRole
            ),
            key=lambda pin: pin.role.value,
        )
    )
    distributions = {
        name: {"minimum": 0, "median": 1, "p95": 4, "maximum": 8}
        for name in ("files", "images", "pages", "bytes", "representations", "segments")
    }
    return {
        "processingPlan": _artifact("urn:docspec:test:processing-plan"),
        "executionProfile": _artifact("urn:docspec:test:execution-profile"),
        "corpus": {
            "identity": "urn:docspec:test:corpus:representative-100k",
            "digest": ZERO_DIGEST,
            "selectionMethod": "stratified-byte-page-media-cost-v1",
        },
        "inputShape": {
            "sampleIdentity": "urn:docspec:test:sample:representative-v1",
            "sampleDigest": ZERO_DIGEST,
            "distributions": distributions,
        },
        "processingGraph": [
            {
                "stageId": "extract-text",
                "stageKind": "extractor",
                "implementationId": "docspec.extractor.text.v1",
                "configurationDigest": ZERO_DIGEST,
                "inputLayerKinds": ["file"],
                "outputLayerKind": "representation.text",
            },
            {
                "stageId": "segment-paragraph",
                "stageKind": "segmenter",
                "implementationId": "docspec.segmenter.paragraph.v1",
                "configurationDigest": ZERO_DIGEST,
                "inputLayerKinds": ["representation.text"],
                "outputLayerKind": "segment.paragraph",
            },
            {
                "stageId": "content-statistics",
                "stageKind": "processor",
                "implementationId": "docspec.processor.content-statistics.v1",
                "configurationDigest": ZERO_DIGEST,
                "inputLayerKinds": ["segment.paragraph"],
                "outputLayerKind": "derived.content-statistics",
            },
        ],
        "resources": {
            "environmentId": "urn:docspec:test:environment:local",
            "docspecVersion": "0.2.1",
            "pythonVersion": "3.12",
            "workerCount": 8,
            "workerCpu": 1,
            "workerMemoryBytes": 8 * 1024**3,
            "coordinatorMemoryBytes": 16 * 1024**3,
        },
        "documentStorePolicy": {
            "maxEntries": 100,
            "maxEstimatedBytes": 1024**3,
            "maxExpectedSegments": 100_000,
            "maxDurationSeconds": 3600,
        },
        "resultSink": {
            "sinkId": "urn:docspec:test:sink:durable",
            "configurationDigest": ZERO_DIGEST,
        },
        "profileSet": ProfileSet(pins).to_dict(),
        "documentCatalog": {
            "implementationId": "docspec.document-catalog.local-manifest.v1",
            "configurationDigest": ZERO_DIGEST,
        },
        "baseRelease": None,
        "placement": {
            "workerRegion": "local",
            "storageRegion": "local",
            "sourceColocated": True,
        },
        "cacheState": "cold",
        "partitionPolicy": {
            "identity": "urn:docspec:test:partition-policy:v1",
            "bucketCount": 64,
            "targetMemberBytes": 64 * 1024**2,
            "hardMaxMemberBytes": 256 * 1024**2,
        },
        "taskPolicy": {
            "policyId": "urn:docspec:test:task-policy:v1",
            "maxInFlightStores": 16,
            "maxAttempts": 3,
            "checkpointIntervalSeconds": 30,
        },
        "targets": {
            "unitCount": 100_000,
            "deadlineSeconds": 24 * 60 * 60,
            "maxWorkerCpu": 256,
            "maxWorkerMemoryBytes": 8 * 1024**3,
            "maxCoordinatorMemoryBytes": 16 * 1024**3,
            "processorTargets": [
                {
                    "processorId": "content-statistics",
                    "deadlineSeconds": 3600,
                    "maxConcurrency": 8,
                    "costEstimate": 0,
                    "providerLimits": [],
                }
            ],
        },
        "acceptanceAuthority": {
            "authorityId": "urn:docspec:test:authority:scale",
            "decisionArtifact": "fixture://evidence/acceptance.json",
            "decisionArtifactDigest": ZERO_DIGEST,
        },
    }


def scale_profile_content() -> dict[str, object]:
    return {
        "workloadKind": "document-processing",
        "workload": document_processing_workload_content(),
    }


def source_catalog_scale_profile_content() -> dict[str, object]:
    implementation = {
        "implementationId": "git+https://example.test/docspec@" + "1" * 40,
        "configurationDigest": ZERO_DIGEST,
    }
    return {
        "workloadKind": "source-catalog",
        "workload": {
            "sourceNativeInputs": [
                _artifact("urn:docspec:test:source-native:a"),
                _artifact("urn:docspec:test:source-native:b"),
            ],
            "catalogPolicy": _artifact("urn:docspec:test:catalog-policy"),
            "requestedUniverse": _artifact("urn:docspec:test:requested-universe"),
            "builder": implementation,
            "verifier": implementation,
            "proofStrategy": {
                "join": implementation,
                "order": implementation,
                "setProof": implementation,
                "maxJoinIds": 4,
                "partitionCount": 256,
                "maxWorkingBytes": 512 * 1024**2,
            },
            "outputProfile": _artifact("urn:docspec:test:catalog-output-profile"),
            "command": _artifact("urn:docspec:test:catalog-command"),
            "referenceMachine": _artifact("urn:docspec:test:reference-machine"),
            "resources": {
                "environmentId": "urn:docspec:test:environment:catalog",
                "docspecVersion": "0.2.1",
                "pythonVersion": "3.12",
                "workerCount": 1,
                "workerCpu": 8,
                "workerMemoryBytes": 8 * 1024**3,
                "coordinatorMemoryBytes": 16 * 1024**3,
            },
            "cacheState": "cold",
            "measurementMethod": _artifact("urn:docspec:test:measurement-method"),
            "ceilings": {
                "maxSourceRecordCount": 100_000,
                "maxSourceBytes": 4 * 1024**3,
                "maxWallTimeMilliseconds": 60 * 60 * 1000,
                "maxPeakResidentMemoryBytes": 16 * 1024**3,
                "maxOutputBytes": 8 * 1024**3,
                "maxPayloadBytesWritten": 8 * 1024**3,
                "maxPublicationBytesWritten": 16 * 1024**2,
                "maxPartitionCount": 256,
            },
            "acceptanceAuthority": {
                "authorityId": "urn:docspec:test:authority:catalog-scale",
                "decisionArtifact": "fixture://evidence/catalog-acceptance.json",
                "decisionArtifactDigest": ZERO_DIGEST,
            },
        },
    }


def _catalog_result_content(profile: ScaleProfile) -> dict[str, object]:
    workload = profile.catalog_workload
    assert workload is not None
    inputs = sorted(
        (
            *workload.source_native_inputs,
            workload.catalog_policy,
            workload.requested_universe,
        ),
        key=lambda item: (item.artifact_id, item.locator, item.digest),
    )
    return {
        "profile": {
            "artifactId": profile.profile_id,
            "locator": PROFILE_LOCATOR,
            "digest": profile.digest,
        },
        "workloadKind": "source-catalog",
        "startedAt": "2026-08-26T12:00:00Z",
        "completedAt": "2026-08-26T12:10:00Z",
        "inputArtifacts": [item.to_dict() for item in inputs],
        "outputArtifacts": [_artifact("urn:docspec:test:source-catalog:output")],
        "metrics": {
            "inputItemCount": 100_000,
            "outputItemCount": 100_000,
            "partitionCount": 256,
            "taskCount": 0,
            "storeCount": 0,
            "releaseCount": 0,
            "inputBytes": 1024**3,
            "outputBytes": 2 * 1024**3,
            "payloadBytesRead": 2 * 1024**3,
            "payloadBytesReused": 0,
            "payloadBytesWritten": 2 * 1024**3,
            "publicationBytesWritten": 1024**2,
            "wallTimeMilliseconds": 10 * 60 * 1000,
            "peakWorkerCpu": 4,
            "peakWorkerMemoryBytes": 4 * 1024**3,
            "peakCoordinatorMemoryBytes": 8 * 1024**3,
            "peakScratchBytes": 1024**3,
        },
        "evidence": [_artifact("urn:docspec:test:scale-evidence:catalog")],
        "firstFailure": None,
        "verdict": "pass",
    }


def _document_result_content(profile: ScaleProfile) -> dict[str, object]:
    workload = profile.document_processing_workload
    assert workload is not None
    return {
        "profile": {
            "artifactId": profile.profile_id,
            "locator": PROFILE_LOCATOR,
            "digest": profile.digest,
        },
        "workloadKind": "document-processing",
        "startedAt": "2026-08-26T12:00:00Z",
        "completedAt": "2026-08-26T13:00:00Z",
        "inputArtifacts": [
            {
                "artifactId": workload.corpus.identity,
                "locator": "fixture://corpora/representative-100k.json",
                "digest": workload.corpus.digest,
            }
        ],
        "outputArtifacts": [_artifact("urn:docspec:test:document-release:output")],
        "metrics": {
            "inputItemCount": 100_000,
            "outputItemCount": 100_000,
            "partitionCount": 64,
            "taskCount": 1000,
            "storeCount": 1000,
            "releaseCount": 1,
            "inputBytes": 1024**3,
            "outputBytes": 2 * 1024**3,
            "payloadBytesRead": 1024**3,
            "payloadBytesReused": 0,
            "payloadBytesWritten": 2 * 1024**3,
            "publicationBytesWritten": 1024**2,
            "wallTimeMilliseconds": 60 * 60 * 1000,
            "peakWorkerCpu": 8,
            "peakWorkerMemoryBytes": 4 * 1024**3,
            "peakCoordinatorMemoryBytes": 8 * 1024**3,
            "peakScratchBytes": 1024**3,
        },
        "evidence": [_artifact("urn:docspec:test:scale-evidence:document")],
        "firstFailure": None,
        "verdict": "pass",
    }


def test_document_processing_scale_profile_remains_one_closed_canonical_variant() -> None:
    profile = ScaleProfile.from_content_dict(scale_profile_content())
    restored = ScaleProfile.from_bytes(profile.to_bytes())

    assert restored == profile
    assert profile.workload_kind is ScaleWorkloadKind.DOCUMENT_PROCESSING
    assert isinstance(profile.workload, ScaleDocumentProcessingWorkload)
    assert profile.to_dict()["formatVersion"] == "2.0"
    assert profile.profile_id.startswith("urn:docspec:scale-profile:v1:")
    assert profile.digest.startswith("sha256:")
    assert profile.workload.targets.unit_count == 100_000
    assert len(profile.workload.profile_set.pins) == 6


def test_source_catalog_profile_pins_every_accepted_section_without_processing_fields() -> None:
    profile = ScaleProfile.from_content_dict(source_catalog_scale_profile_content())
    restored = ScaleProfile.from_bytes(profile.to_bytes())

    assert restored == profile
    assert profile.workload_kind is ScaleWorkloadKind.SOURCE_CATALOG
    assert isinstance(profile.workload, ScaleSourceCatalogWorkload)
    assert len(profile.workload.source_native_inputs) == 2
    assert profile.workload.proof_strategy.max_join_ids == 4
    assert profile.workload.ceilings.max_source_record_count == 100_000
    assert "processingPlan" not in profile.to_dict()["workload"]


def test_scale_profile_identity_changes_with_variant_inputs_or_ceilings() -> None:
    original = source_catalog_scale_profile_content()
    changed_input = deepcopy(original)
    changed_input["workload"]["sourceNativeInputs"][0]["digest"] = "sha256:" + "1" * 64  # type: ignore[index]
    changed_ceiling = deepcopy(original)
    changed_ceiling["workload"]["ceilings"]["maxSourceRecordCount"] = 1_000_000  # type: ignore[index]

    identities = {
        ScaleProfile.from_content_dict(value).profile_id
        for value in (original, changed_input, changed_ceiling)
    }
    assert len(identities) == 3


def test_scale_profile_union_rejects_old_mixed_or_unknown_shapes() -> None:
    source = source_catalog_scale_profile_content()
    source["workload"]["processingGraph"] = []  # type: ignore[index]
    with pytest.raises(ProfileError, match="closed shape"):
        ScaleProfile.from_content_dict(source)

    mixed = scale_profile_content()
    mixed["workloadKind"] = "source-catalog"
    with pytest.raises(ProfileError, match="closed shape"):
        ScaleProfile.from_content_dict(mixed)

    profile = ScaleProfile.from_content_dict(scale_profile_content()).to_dict()
    profile["formatVersion"] = "1.1"
    with pytest.raises(ProfileError, match="unknown format"):
        ScaleProfile.from_dict(profile)

    unknown = scale_profile_content()
    unknown["scheduler"] = {"name": "not part of the scale profile"}
    with pytest.raises(ProfileError, match="closed shape"):
        ScaleProfile.from_content_dict(unknown)


def test_document_variant_retains_processor_and_resource_validation() -> None:
    missing_target = scale_profile_content()
    missing_target["workload"]["targets"]["processorTargets"] = []  # type: ignore[index]
    with pytest.raises(ProfileError, match="every processor stage"):
        ScaleProfile.from_content_dict(missing_target)

    fractional = scale_profile_content()
    fractional["workload"]["resources"]["workerCpu"] = 0.5  # type: ignore[index]
    with pytest.raises(ProfileError, match="positive integer"):
        ScaleProfile.from_content_dict(fractional)

    reversed_distribution = scale_profile_content()
    pages = reversed_distribution["workload"]["inputShape"]["distributions"]["pages"]  # type: ignore[index]
    pages["p95"] = 99  # type: ignore[index]
    pages["maximum"] = 9  # type: ignore[index]
    with pytest.raises(ProfileError, match="must be ordered"):
        ScaleProfile.from_content_dict(reversed_distribution)


def test_scale_result_is_closed_canonical_and_binds_complete_catalog_evidence() -> None:
    profile = ScaleProfile.from_content_dict(source_catalog_scale_profile_content())
    result = ScaleResult.from_content_dict(_catalog_result_content(profile))
    restored = ScaleResult.from_bytes(result.to_bytes())

    assert restored == result
    assert result.result_id.startswith("urn:docspec:scale-result:v1:")
    result.verify_profile(profile, profile_locator=PROFILE_LOCATOR)

    with pytest.raises(IntegrityError, match="different sealed profile"):
        result.verify_profile(profile, profile_locator="fixture://different/profile.json")


def test_scale_result_rejects_false_passes_for_both_workload_variants() -> None:
    catalog_profile = ScaleProfile.from_content_dict(source_catalog_scale_profile_content())
    catalog_value = _catalog_result_content(catalog_profile)
    catalog_value["metrics"]["peakCoordinatorMemoryBytes"] = 17 * 1024**3  # type: ignore[index]
    catalog_result = ScaleResult.from_content_dict(catalog_value)
    with pytest.raises(IntegrityError, match="profile ceilings"):
        catalog_result.verify_profile(catalog_profile, profile_locator=PROFILE_LOCATOR)

    document_profile = ScaleProfile.from_content_dict(scale_profile_content())
    document_value = _document_result_content(document_profile)
    document_value["metrics"]["peakWorkerCpu"] = 9  # type: ignore[index]
    document_result = ScaleResult.from_content_dict(document_value)
    with pytest.raises(IntegrityError, match="document scale result exceeds"):
        document_result.verify_profile(document_profile, profile_locator=PROFILE_LOCATOR)

    target_limited_profile_value = scale_profile_content()
    target_limited_profile_value["workload"]["targets"]["maxWorkerCpu"] = 7  # type: ignore[index]
    target_limited_profile = ScaleProfile.from_content_dict(target_limited_profile_value)
    target_limited_result = ScaleResult.from_content_dict(
        _document_result_content(target_limited_profile)
    )
    with pytest.raises(IntegrityError, match="document scale result exceeds"):
        target_limited_result.verify_profile(
            target_limited_profile,
            profile_locator=PROFILE_LOCATOR,
        )


def test_scale_result_fails_closed_for_identity_verdict_and_canonical_bytes() -> None:
    profile = ScaleProfile.from_content_dict(source_catalog_scale_profile_content())
    result = ScaleResult.from_content_dict(_catalog_result_content(profile))
    tampered = result.to_dict()
    tampered["metrics"]["outputItemCount"] = 99
    with pytest.raises(ProfileError, match="identity differs"):
        ScaleResult.from_dict(tampered)

    false_failure = _catalog_result_content(profile)
    false_failure["verdict"] = "fail"
    with pytest.raises(ProfileError, match="must declare its first failure"):
        ScaleResult.from_content_dict(false_failure)

    with pytest.raises(IntegrityError, match="canonical"):
        ScaleResult.from_bytes(result.to_bytes().rstrip(b"\n"))


def test_checked_in_schemas_admit_both_profile_variants_and_result_evidence() -> None:
    profile_schema = json.loads(
        (REPO_ROOT / "conformance" / "scale-profile.schema.json").read_text(encoding="utf-8")
    )
    result_schema = json.loads(
        (REPO_ROOT / "conformance" / "scale-result.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(profile_schema)
    jsonschema.Draft202012Validator.check_schema(result_schema)

    for content in (scale_profile_content(), source_catalog_scale_profile_content()):
        profile = ScaleProfile.from_content_dict(content)
        jsonschema.Draft202012Validator(profile_schema).validate(profile.to_dict())

    catalog_profile = ScaleProfile.from_content_dict(source_catalog_scale_profile_content())
    result = ScaleResult.from_content_dict(_catalog_result_content(catalog_profile))
    jsonschema.Draft202012Validator(result_schema).validate(result.to_dict())
