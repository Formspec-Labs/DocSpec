"""Generate conformance/scale-profile.schema.json from docspec.domain.scale.

Before this generator existed, the schema was a 13.7 KB hand-typed JSON Schema
document with no validator in the dependency tree (``dependencies = []`` in
pyproject.toml) and no consumer other than a test that hand-asserted about
twenty of its properties against literals. The real constraints live in
``docspec/domain/scale.py``'s dataclasses and ``__post_init__`` validators,
so the schema was free to drift from them -- and had: several fields that
``scale.py`` enforces as strict positive/non-negative Python ``int`` (via
``_positive_integer``/``_non_negative_integer``, which reject floats and
bools) were declared in the old schema as JSON Schema ``"number"``, which
accepts floats. A schema is normative text; it should not accept values the
real parser rejects.

Field *names* and *order* for every object fragment below are read directly
off the live dataclasses via ``dataclasses.fields()`` and asserted against
the hand-declared "kind" table before a schema is built, so a field added
to, removed from, or renamed in ``scale.py`` without a matching update here
raises immediately instead of the checked-in schema quietly going stale.
The "kind" of each field (text, sha256 digest, positive integer, ...) still
has to be declared here because that comes from validator calls inside
``__post_init__`` bodies, not from type hints -- there is no way to reflect
it out of the dataclass alone.

Usage:
    uv run python -m tools.generate_scale_profile_schema > conformance/scale-profile.schema.json

tests/test_machine_files.py imports ``build_schema`` and asserts the checked-in
file is exactly what this module currently produces.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from docspec.domain.profiles import ProfilePin, ProfileRole
from docspec.domain.references import DocumentReleaseRef
from docspec.domain.scale import (
    SCALE_PROFILE_FORMAT,
    SCALE_PROFILE_VERSION,
    ScaleAcceptanceAuthority,
    ScaleArtifactPin,
    ScaleCacheState,
    ScaleCorpus,
    ScaleDistribution,
    ScaleDocumentStorePolicy,
    ScaleImplementationPin,
    ScaleInputShape,
    ScalePartitionPolicy,
    ScalePlacement,
    ScaleProcessingStage,
    ScaleProcessorTarget,
    ScaleProfile,
    ScaleProviderLimit,
    ScaleResources,
    ScaleResultSinkPin,
    ScaleStageKind,
    ScaleTargets,
    ScaleTaskPolicy,
    _DISTRIBUTION_KINDS,
    _SCALE_CONTENT_FIELDS,
)

SCHEMA_ID = "https://docspec.org/schemas/scale-profile/1.1.json"

# Reusable JSON Schema fragments matching scale.py's field-level validators.
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
    """Build an object schema for `cls`, asserting the table matches its live fields."""

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


def _sorted_unique_array(item: dict[str, Any], *, min_items: int | None = 1) -> dict[str, Any]:
    fragment: dict[str, Any] = {"type": "array", "uniqueItems": True, "items": item}
    if min_items is not None:
        fragment["minItems"] = min_items
    return fragment


def build_schema() -> dict[str, Any]:
    artifact_pin = _object_fragment(
        ScaleArtifactPin,
        {"artifactId": _TEXT, "locator": _TEXT, "digest": _SHA256},
    )
    distribution = _object_fragment(
        ScaleDistribution,
        {"minimum": _NON_NEGATIVE_INT, "median": _NON_NEGATIVE_INT, "p95": _NON_NEGATIVE_INT, "maximum": _NON_NEGATIVE_INT},
    )
    implementation_pin = _object_fragment(
        ScaleImplementationPin,
        {"implementationId": _TEXT, "configurationDigest": _SHA256},
    )
    provider_limit = _object_fragment(
        ScaleProviderLimit,
        {"name": _TEXT, "unit": _TEXT, "maximum": _POSITIVE_INT},
    )
    profile_pin = _object_fragment(
        ProfilePin,
        {
            "role": {"enum": [role.value for role in ProfileRole]},
            "profileId": _TEXT,
            "version": _TEXT,
            "implementationId": _TEXT,
            "configurationDigest": _SHA256,
            "descriptionDigest": _SHA256,
            "capabilities": _sorted_unique_array(_TEXT),
        },
    )

    corpus = _object_fragment(
        ScaleCorpus,
        {"identity": _TEXT, "digest": _SHA256, "selectionMethod": _TEXT},
    )
    input_shape_distribution_names = [_camel(name) for name in _DISTRIBUTION_KINDS]
    input_shape = _object_fragment(
        ScaleInputShape,
        {
            "sampleIdentity": _TEXT,
            "sampleDigest": _SHA256,
            "distributions": {
                "type": "object",
                "additionalProperties": False,
                "required": input_shape_distribution_names,
                "properties": {name: {"$ref": "#/$defs/distribution"} for name in input_shape_distribution_names},
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
            "inputLayerKinds": _sorted_unique_array(_TEXT),
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
            "providerLimits": _sorted_unique_array({"$ref": "#/$defs/providerLimit"}, min_items=None),
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
            "processorTargets": {"type": "array", "items": processor_target},
        },
    )
    acceptance_authority = _object_fragment(
        ScaleAcceptanceAuthority,
        {"authorityId": _TEXT, "decisionArtifact": _TEXT, "decisionArtifactDigest": _SHA256},
    )

    profile_role_defs: dict[str, Any] = {}
    contains_defs: dict[str, Any] = {}
    for role in ProfileRole:
        # def names are derived from the enum *value* (e.g. "DocumentStorePersistenceProfile"),
        # not the shorter member name ("DOCUMENT_STORE") -- they differ for this role.
        pin_def_name = role.value[0].lower() + role.value[1:] + "Pin"
        profile_role_defs[pin_def_name] = {
            "type": "object",
            "required": ["role"],
            "properties": {"role": {"const": role.value}},
        }
        has_def_name = f"has{role.value}"
        contains_defs[has_def_name] = {
            "contains": {"$ref": f"#/$defs/{pin_def_name}"},
            "minContains": 1,
            "maxContains": 1,
        }

    profile_set_properties = {
        "profileSetId": _TEXT,
        "pins": {
            "type": "array",
            "minItems": len(tuple(ProfileRole)),
            "maxItems": len(tuple(ProfileRole)),
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/profilePin"},
            "allOf": [{"$ref": f"#/$defs/{name}"} for name in contains_defs],
        },
    }

    top_level_properties: dict[str, Any] = {
        "format": {"const": SCALE_PROFILE_FORMAT},
        "formatVersion": {"const": SCALE_PROFILE_VERSION},
        "profileId": _TEXT,
        "processingPlan": {"$ref": "#/$defs/artifactPin"},
        "executionProfile": {"$ref": "#/$defs/artifactPin"},
        "corpus": corpus,
        "inputShape": input_shape,
        "processingGraph": {"type": "array", "minItems": 1, "items": processing_stage},
        "resources": resources,
        "documentStorePolicy": document_store_policy,
        "resultSink": result_sink,
        "profileSet": {
            "type": "object",
            "additionalProperties": False,
            "required": ["profileSetId", "pins"],
            "properties": profile_set_properties,
        },
        "documentCatalog": {"$ref": "#/$defs/implementationPin"},
        "baseRelease": {"oneOf": [{"type": "null"}, base_release]},
        "placement": placement,
        "cacheState": {"enum": [state.value for state in ScaleCacheState]},
        "partitionPolicy": partition_policy,
        "taskPolicy": task_policy,
        "targets": targets,
        "acceptanceAuthority": acceptance_authority,
    }

    live_top_level = {"format", "formatVersion", "profileId"} | set(_SCALE_CONTENT_FIELDS)
    if set(top_level_properties) != live_top_level:
        missing = sorted(live_top_level - set(top_level_properties))
        extra = sorted(set(top_level_properties) - live_top_level)
        raise AssertionError(f"top-level schema is out of sync with ScaleProfile: missing={missing} extra={extra}")

    # Field order matters for a stable, reviewable diff. `_SCALE_CONTENT_FIELDS` is a
    # set (order is not meaningful there); the real, deterministic order is the
    # ScaleProfile dataclass's own declared field order.
    required_order = [
        "format",
        "formatVersion",
        "profileId",
        *(_camel(field.name) for field in dataclasses.fields(ScaleProfile) if _camel(field.name) in _SCALE_CONTENT_FIELDS),
    ]

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "DocSpec ScaleProfile",
        "type": "object",
        "additionalProperties": False,
        "required": required_order,
        "properties": {name: top_level_properties[name] for name in required_order},
        "$defs": {
            "sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "distribution": distribution,
            "implementationPin": implementation_pin,
            "artifactPin": artifact_pin,
            "providerLimit": provider_limit,
            "profilePin": profile_pin,
            **contains_defs,
            **profile_role_defs,
        },
    }


def schema_bytes() -> bytes:
    return (json.dumps(build_schema(), indent=2) + "\n").encode("utf-8")


def main() -> None:
    import sys

    sys.stdout.buffer.write(schema_bytes())


if __name__ == "__main__":
    main()
