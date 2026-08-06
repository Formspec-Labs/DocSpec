from __future__ import annotations

from copy import deepcopy

import pytest

from docspec.domain.profiles import ProfilePin, ProfileRole, ProfileSet
from docspec.domain.scale import ScaleProfile
from docspec.errors import IntegrityError, ProfileError

ZERO_DIGEST = "sha256:" + "0" * 64


def scale_profile_content() -> dict[str, object]:
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
        "processingPlan": {
            "artifactId": "urn:docspec:test:processing-plan",
            "locator": "s3://controls/plans/plan.json",
            "digest": ZERO_DIGEST,
        },
        "executionProfile": {
            "artifactId": "urn:docspec:test:execution-profile",
            "locator": "s3://controls/execution/profile.json",
            "digest": ZERO_DIGEST,
        },
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
            "docspecVersion": "0.2.0",
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
        "resultSink": {"sinkId": "urn:docspec:test:sink:durable", "configurationDigest": ZERO_DIGEST},
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
            "decisionArtifact": "s3://evidence/acceptance.json",
            "decisionArtifactDigest": ZERO_DIGEST,
        },
    }


def test_scale_profile_is_closed_canonical_typed_and_content_addressed() -> None:
    profile = ScaleProfile.from_content_dict(scale_profile_content())

    restored = ScaleProfile.from_bytes(profile.to_bytes())

    assert restored == profile
    assert profile.profile_id.startswith("urn:docspec:scale-profile:v1:")
    assert profile.to_dict()["profileId"] == profile.profile_id
    assert profile.digest.startswith("sha256:")
    assert profile.targets.unit_count == 100_000
    assert profile.processing_plan.artifact_id == "urn:docspec:test:processing-plan"
    assert len(profile.profile_set.pins) == 6


def test_scale_profile_identity_changes_with_claim_inputs_or_targets() -> None:
    original = scale_profile_content()
    changed_corpus = deepcopy(original)
    changed_corpus["corpus"]["identity"] = "urn:docspec:test:corpus:representative-1m"  # type: ignore[index]
    changed_target = deepcopy(original)
    changed_target["targets"]["unitCount"] = 1_000_000  # type: ignore[index]

    identities = {
        ScaleProfile.from_content_dict(value).profile_id for value in (original, changed_corpus, changed_target)
    }

    assert len(identities) == 3


def test_scale_profile_fails_closed_for_tampering_unknown_fields_and_missing_processor_targets() -> None:
    profile = ScaleProfile.from_content_dict(scale_profile_content())
    tampered = profile.to_dict()
    tampered["targets"]["unitCount"] = 1_000_000
    with pytest.raises(ProfileError, match="identity differs"):
        ScaleProfile.from_dict(tampered)

    unknown = scale_profile_content()
    unknown["scheduler"] = {"name": "not part of the scale profile"}
    with pytest.raises(ProfileError, match="closed shape"):
        ScaleProfile.from_content_dict(unknown)

    missing_target = scale_profile_content()
    missing_target["targets"]["processorTargets"] = []  # type: ignore[index]
    with pytest.raises(ProfileError, match="every processor stage"):
        ScaleProfile.from_content_dict(missing_target)

    with pytest.raises(IntegrityError, match="canonical"):
        ScaleProfile.from_bytes(profile.to_bytes().rstrip(b"\n"))


def test_scale_profile_rejects_ambiguous_or_impossible_bounds() -> None:
    fractional = scale_profile_content()
    fractional["resources"]["workerCpu"] = 0.5  # type: ignore[index]
    with pytest.raises(ProfileError, match="positive integer"):
        ScaleProfile.from_content_dict(fractional)

    reversed_distribution = scale_profile_content()
    distribution = reversed_distribution["inputShape"]["distributions"]["pages"]  # type: ignore[index]
    distribution["p95"] = 99  # type: ignore[index]
    distribution["maximum"] = 9  # type: ignore[index]
    with pytest.raises(ProfileError, match="must be ordered"):
        ScaleProfile.from_content_dict(reversed_distribution)

    oversized_target = scale_profile_content()
    oversized_target["partitionPolicy"]["targetMemberBytes"] = 1024  # type: ignore[index]
    oversized_target["partitionPolicy"]["hardMaxMemberBytes"] = 512  # type: ignore[index]
    with pytest.raises(ProfileError, match="must not be less"):
        ScaleProfile.from_content_dict(oversized_target)
