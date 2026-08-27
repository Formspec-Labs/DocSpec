"""Generate the checked-in ScaleProfile and ScaleResult JSON Schemas.

Object field names and order are reflected from the live dataclasses. The small
field-kind tables below remain explicit because Python annotations do not encode
positive-integer, digest, ordering, or closed-union constraints.

Usage:
    uv run python -m tools.generate_scale_profile_schema
    uv run python -m tools.generate_scale_profile_schema --result
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from typing import Any

from docspec.domain.profiles import ProfilePin, ProfileRole
from docspec.domain.references import DocumentReleaseRef
from docspec.domain.scale import (
    SCALE_PROFILE_FORMAT,
    SCALE_PROFILE_VERSION,
    SCALE_RESULT_FORMAT,
    SCALE_RESULT_VERSION,
    ScaleAcceptanceAuthority,
    ScaleArtifactPin,
    ScaleCacheState,
    ScaleCatalogCeilings,
    ScaleCatalogProofStrategy,
    ScaleCorpus,
    ScaleDistribution,
    ScaleDocumentProcessingWorkload,
    ScaleDocumentStorePolicy,
    ScaleFirstFailure,
    ScaleImplementationPin,
    ScaleInputShape,
    ScalePartitionPolicy,
    ScalePlacement,
    ScaleProcessingStage,
    ScaleProcessorTarget,
    ScaleProfile,
    ScaleProviderLimit,
    ScaleResources,
    ScaleResult,
    ScaleResultMetrics,
    ScaleResultSinkPin,
    ScaleSourceCatalogWorkload,
    ScaleStageKind,
    ScaleTargets,
    ScaleTaskPolicy,
    ScaleVerdict,
    ScaleWorkloadKind,
    _DISTRIBUTION_KINDS,
)

SCHEMA_ID = "https://docspec.org/schemas/scale-profile/2.0.json"
RESULT_SCHEMA_ID = "https://docspec.org/schemas/scale-result/1.0.json"

_TEXT: dict[str, Any] = {"type": "string", "minLength": 1}
_SHA256: dict[str, Any] = {"$ref": "#/$defs/sha256"}
_POSITIVE_INT: dict[str, Any] = {"type": "integer", "minimum": 1}
_NON_NEGATIVE_INT: dict[str, Any] = {"type": "integer", "minimum": 0}
_BOOLEAN: dict[str, Any] = {"type": "boolean"}


def _camel(field_name: str) -> str:
    head, *rest = field_name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _object_fragment(
    cls: type,
    fields_by_json_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    live = [_camel(field.name) for field in dataclasses.fields(cls)]
    declared = set(fields_by_json_name)
    if set(live) != declared:
        missing = sorted(set(live) - declared)
        extra = sorted(declared - set(live))
        raise AssertionError(
            f"{cls.__name__} schema table is out of sync with its dataclass fields: "
            f"missing={missing} extra={extra}"
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": live,
        "properties": {name: fields_by_json_name[name] for name in live},
    }


def _array(
    item: dict[str, Any],
    *,
    min_items: int | None = 1,
    unique: bool = False,
) -> dict[str, Any]:
    fragment: dict[str, Any] = {"type": "array", "items": item}
    if min_items is not None:
        fragment["minItems"] = min_items
    if unique:
        fragment["uniqueItems"] = True
    return fragment


def _profile_set_fragments() -> tuple[dict[str, Any], dict[str, Any]]:
    profile_pin = _object_fragment(
        ProfilePin,
        {
            "role": {"enum": [role.value for role in ProfileRole]},
            "profileId": _TEXT,
            "version": _TEXT,
            "implementationId": _TEXT,
            "configurationDigest": _SHA256,
            "descriptionDigest": _SHA256,
            "capabilities": _array(_TEXT, unique=True),
        },
    )
    definitions: dict[str, Any] = {"profilePin": profile_pin}
    contains: list[dict[str, Any]] = []
    for role in ProfileRole:
        pin_name = role.value[0].lower() + role.value[1:] + "Pin"
        definitions[pin_name] = {
            "type": "object",
            "required": ["role"],
            "properties": {"role": {"const": role.value}},
        }
        has_name = f"has{role.value}"
        definitions[has_name] = {
            "contains": {"$ref": f"#/$defs/{pin_name}"},
            "minContains": 1,
            "maxContains": 1,
        }
        contains.append({"$ref": f"#/$defs/{has_name}"})
    profile_set = {
        "type": "object",
        "additionalProperties": False,
        "required": ["profileSetId", "pins"],
        "properties": {
            "profileSetId": _TEXT,
            "pins": {
                "type": "array",
                "minItems": len(tuple(ProfileRole)),
                "maxItems": len(tuple(ProfileRole)),
                "uniqueItems": True,
                "items": {"$ref": "#/$defs/profilePin"},
                "allOf": contains,
            },
        },
    }
    return profile_set, definitions


def _profile_fragments() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    artifact_pin = _object_fragment(
        ScaleArtifactPin,
        {"artifactId": _TEXT, "locator": _TEXT, "digest": _SHA256},
    )
    implementation_pin = _object_fragment(
        ScaleImplementationPin,
        {"implementationId": _TEXT, "configurationDigest": _SHA256},
    )
    distribution = _object_fragment(
        ScaleDistribution,
        {
            "minimum": _NON_NEGATIVE_INT,
            "median": _NON_NEGATIVE_INT,
            "p95": _NON_NEGATIVE_INT,
            "maximum": _NON_NEGATIVE_INT,
        },
    )
    provider_limit = _object_fragment(
        ScaleProviderLimit,
        {"name": _TEXT, "unit": _TEXT, "maximum": _POSITIVE_INT},
    )
    profile_set, profile_definitions = _profile_set_fragments()

    corpus = _object_fragment(
        ScaleCorpus,
        {"identity": _TEXT, "digest": _SHA256, "selectionMethod": _TEXT},
    )
    distribution_names = list(_DISTRIBUTION_KINDS)
    input_shape = _object_fragment(
        ScaleInputShape,
        {
            "sampleIdentity": _TEXT,
            "sampleDigest": _SHA256,
            "distributions": {
                "type": "object",
                "additionalProperties": False,
                "required": distribution_names,
                "properties": {
                    name: {"$ref": "#/$defs/distribution"}
                    for name in distribution_names
                },
            },
        },
    )
    processing_stage = _object_fragment(
        ScaleProcessingStage,
        {
            "stageId": _TEXT,
            "stageKind": {"enum": [kind.value for kind in ScaleStageKind]},
            "implementationId": _TEXT,
            "configurationDigest": _SHA256,
            "inputLayerKinds": _array(_TEXT, unique=True),
            "outputLayerKind": _TEXT,
        },
    )
    resources = _object_fragment(
        ScaleResources,
        {
            "environmentId": _TEXT,
            "docspecVersion": _TEXT,
            "pythonVersion": _TEXT,
            "workerCount": _POSITIVE_INT,
            "workerCpu": _POSITIVE_INT,
            "workerMemoryBytes": _POSITIVE_INT,
            "coordinatorMemoryBytes": _POSITIVE_INT,
        },
    )
    document_store_policy = _object_fragment(
        ScaleDocumentStorePolicy,
        {
            "maxEntries": _POSITIVE_INT,
            "maxEstimatedBytes": _POSITIVE_INT,
            "maxExpectedSegments": _POSITIVE_INT,
            "maxDurationSeconds": _POSITIVE_INT,
        },
    )
    result_sink = _object_fragment(
        ScaleResultSinkPin,
        {"sinkId": _TEXT, "configurationDigest": _SHA256},
    )
    base_release = _object_fragment(
        DocumentReleaseRef,
        {"releaseId": _TEXT, "locator": _TEXT, "digest": _SHA256},
    )
    placement = _object_fragment(
        ScalePlacement,
        {"workerRegion": _TEXT, "storageRegion": _TEXT, "sourceColocated": _BOOLEAN},
    )
    partition_policy = _object_fragment(
        ScalePartitionPolicy,
        {
            "identity": _TEXT,
            "bucketCount": _POSITIVE_INT,
            "targetMemberBytes": _POSITIVE_INT,
            "hardMaxMemberBytes": _POSITIVE_INT,
        },
    )
    task_policy = _object_fragment(
        ScaleTaskPolicy,
        {
            "policyId": _TEXT,
            "maxInFlightStores": _POSITIVE_INT,
            "maxAttempts": _POSITIVE_INT,
            "checkpointIntervalSeconds": _POSITIVE_INT,
        },
    )
    processor_target = _object_fragment(
        ScaleProcessorTarget,
        {
            "processorId": _TEXT,
            "deadlineSeconds": _POSITIVE_INT,
            "maxConcurrency": _POSITIVE_INT,
            "costEstimate": _NON_NEGATIVE_INT,
            "providerLimits": _array(
                {"$ref": "#/$defs/providerLimit"},
                min_items=None,
                unique=True,
            ),
        },
    )
    targets = _object_fragment(
        ScaleTargets,
        {
            "unitCount": _POSITIVE_INT,
            "deadlineSeconds": _POSITIVE_INT,
            "maxWorkerCpu": _POSITIVE_INT,
            "maxWorkerMemoryBytes": _POSITIVE_INT,
            "maxCoordinatorMemoryBytes": _POSITIVE_INT,
            "processorTargets": _array(processor_target, min_items=None),
        },
    )
    acceptance_authority = _object_fragment(
        ScaleAcceptanceAuthority,
        {
            "authorityId": _TEXT,
            "decisionArtifact": _TEXT,
            "decisionArtifactDigest": _SHA256,
        },
    )
    document_workload = _object_fragment(
        ScaleDocumentProcessingWorkload,
        {
            "processingPlan": {"$ref": "#/$defs/artifactPin"},
            "executionProfile": {"$ref": "#/$defs/artifactPin"},
            "corpus": corpus,
            "inputShape": input_shape,
            "processingGraph": _array(processing_stage),
            "resources": resources,
            "documentStorePolicy": document_store_policy,
            "resultSink": result_sink,
            "profileSet": profile_set,
            "documentCatalog": {"$ref": "#/$defs/implementationPin"},
            "baseRelease": {"oneOf": [{"type": "null"}, base_release]},
            "placement": placement,
            "cacheState": {"enum": [state.value for state in ScaleCacheState]},
            "partitionPolicy": partition_policy,
            "taskPolicy": task_policy,
            "targets": targets,
            "acceptanceAuthority": acceptance_authority,
        },
    )

    proof_strategy = _object_fragment(
        ScaleCatalogProofStrategy,
        {
            "join": {"$ref": "#/$defs/implementationPin"},
            "order": {"$ref": "#/$defs/implementationPin"},
            "setProof": {"$ref": "#/$defs/implementationPin"},
            "maxJoinIds": _NON_NEGATIVE_INT,
            "partitionCount": _POSITIVE_INT,
            "maxWorkingBytes": _POSITIVE_INT,
        },
    )
    ceilings = _object_fragment(
        ScaleCatalogCeilings,
        {
            "maxSourceRecordCount": _POSITIVE_INT,
            "maxSourceBytes": _POSITIVE_INT,
            "maxWallTimeMilliseconds": _POSITIVE_INT,
            "maxPeakResidentMemoryBytes": _POSITIVE_INT,
            "maxOutputBytes": _POSITIVE_INT,
            "maxPayloadBytesWritten": _NON_NEGATIVE_INT,
            "maxPublicationBytesWritten": _NON_NEGATIVE_INT,
            "maxPartitionCount": _POSITIVE_INT,
        },
    )
    catalog_workload = _object_fragment(
        ScaleSourceCatalogWorkload,
        {
            "sourceNativeInputs": _array(
                {"$ref": "#/$defs/artifactPin"},
                unique=True,
            ),
            "catalogPolicy": {"$ref": "#/$defs/artifactPin"},
            "requestedUniverse": {"$ref": "#/$defs/artifactPin"},
            "builder": {"$ref": "#/$defs/implementationPin"},
            "verifier": {"$ref": "#/$defs/implementationPin"},
            "proofStrategy": proof_strategy,
            "outputProfile": {"$ref": "#/$defs/artifactPin"},
            "command": {"$ref": "#/$defs/artifactPin"},
            "referenceMachine": {"$ref": "#/$defs/artifactPin"},
            "resources": resources,
            "cacheState": {
                "enum": [ScaleCacheState.COLD.value, ScaleCacheState.WARM.value]
            },
            "measurementMethod": {"$ref": "#/$defs/artifactPin"},
            "ceilings": ceilings,
            "acceptanceAuthority": acceptance_authority,
        },
    )
    definitions = {
        "sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "artifactPin": artifact_pin,
        "implementationPin": implementation_pin,
        "distribution": distribution,
        "providerLimit": provider_limit,
        **profile_definitions,
    }
    return document_workload, catalog_workload, definitions


def _profile_branch(
    *,
    workload_kind: ScaleWorkloadKind,
    workload_schema: dict[str, Any],
) -> dict[str, Any]:
    required = ["format", "formatVersion", "profileId", "workloadKind", "workload"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "format": {"const": SCALE_PROFILE_FORMAT},
            "formatVersion": {"const": SCALE_PROFILE_VERSION},
            "profileId": _TEXT,
            "workloadKind": {"const": workload_kind.value},
            "workload": workload_schema,
        },
    }


def build_schema() -> dict[str, Any]:
    if [_camel(field.name) for field in dataclasses.fields(ScaleProfile)] != [
        "workloadKind",
        "workload",
    ]:
        raise AssertionError("ScaleProfile union fields changed without a schema update")
    document_workload, catalog_workload, definitions = _profile_fragments()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "DocSpec ScaleProfile",
        "oneOf": [
            _profile_branch(
                workload_kind=ScaleWorkloadKind.DOCUMENT_PROCESSING,
                workload_schema=document_workload,
            ),
            _profile_branch(
                workload_kind=ScaleWorkloadKind.SOURCE_CATALOG,
                workload_schema=catalog_workload,
            ),
        ],
        "$defs": definitions,
    }


def build_result_schema() -> dict[str, Any]:
    artifact_pin = _object_fragment(
        ScaleArtifactPin,
        {"artifactId": _TEXT, "locator": _TEXT, "digest": _SHA256},
    )
    metrics = _object_fragment(
        ScaleResultMetrics,
        {
            "inputItemCount": _NON_NEGATIVE_INT,
            "outputItemCount": _NON_NEGATIVE_INT,
            "partitionCount": _NON_NEGATIVE_INT,
            "taskCount": _NON_NEGATIVE_INT,
            "storeCount": _NON_NEGATIVE_INT,
            "releaseCount": _NON_NEGATIVE_INT,
            "inputBytes": _NON_NEGATIVE_INT,
            "outputBytes": _NON_NEGATIVE_INT,
            "payloadBytesRead": _NON_NEGATIVE_INT,
            "payloadBytesReused": _NON_NEGATIVE_INT,
            "payloadBytesWritten": _NON_NEGATIVE_INT,
            "publicationBytesWritten": _NON_NEGATIVE_INT,
            "wallTimeMilliseconds": _POSITIVE_INT,
            "peakWorkerCpu": _POSITIVE_INT,
            "peakWorkerMemoryBytes": _POSITIVE_INT,
            "peakCoordinatorMemoryBytes": _POSITIVE_INT,
            "peakScratchBytes": _NON_NEGATIVE_INT,
        },
    )
    first_failure = _object_fragment(
        ScaleFirstFailure,
        {
            "failureCode": _TEXT,
            "stage": _TEXT,
            "evidence": {"$ref": "#/$defs/artifactPin"},
        },
    )
    result_fields = [_camel(field.name) for field in dataclasses.fields(ScaleResult)]
    field_schemas: dict[str, Any] = {
        "profile": {"$ref": "#/$defs/artifactPin"},
        "workloadKind": {"enum": [kind.value for kind in ScaleWorkloadKind]},
        "startedAt": _TEXT,
        "completedAt": _TEXT,
        "inputArtifacts": _array({"$ref": "#/$defs/artifactPin"}, unique=True),
        "outputArtifacts": _array(
            {"$ref": "#/$defs/artifactPin"},
            min_items=None,
            unique=True,
        ),
        "metrics": metrics,
        "evidence": _array({"$ref": "#/$defs/artifactPin"}, unique=True),
        "firstFailure": {"oneOf": [{"type": "null"}, first_failure]},
        "verdict": {"enum": [verdict.value for verdict in ScaleVerdict]},
    }
    if set(result_fields) != set(field_schemas):
        raise AssertionError("ScaleResult fields changed without a schema update")
    required = ["format", "formatVersion", "resultId", *result_fields]
    properties = {
        "format": {"const": SCALE_RESULT_FORMAT},
        "formatVersion": {"const": SCALE_RESULT_VERSION},
        "resultId": _TEXT,
        **{name: field_schemas[name] for name in result_fields},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RESULT_SCHEMA_ID,
        "title": "DocSpec ScaleResult",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
        "allOf": [
            {
                "if": {"properties": {"verdict": {"const": ScaleVerdict.PASS.value}}},
                "then": {
                    "properties": {
                        "firstFailure": {"type": "null"},
                        "outputArtifacts": {"minItems": 1},
                    }
                },
                "else": {"properties": {"firstFailure": first_failure}},
            }
        ],
        "$defs": {
            "sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "artifactPin": artifact_pin,
        },
    }


def schema_bytes() -> bytes:
    return (json.dumps(build_schema(), indent=2) + "\n").encode("utf-8")


def result_schema_bytes() -> bytes:
    return (json.dumps(build_result_schema(), indent=2) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", action="store_true")
    args = parser.parse_args()
    import sys

    sys.stdout.buffer.write(result_schema_bytes() if args.result else schema_bytes())


if __name__ == "__main__":
    main()
