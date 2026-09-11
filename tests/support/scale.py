"""Shared scale fixtures, extracted from tests.test_scale_profile."""

from __future__ import annotations

from docspec.domain.profiles import ProfilePin, ProfileRole, ProfileSet

ZERO_DIGEST = "sha256:" + "0" * 64


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
