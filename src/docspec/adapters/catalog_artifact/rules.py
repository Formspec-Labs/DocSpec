"""Wire names, bounds, partition identities, and source-catalog pins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rulespec_artifacts import (
    MemberDescriptor,
    Producer,
    Supersedes,
    canonical_json_bytes,
    sha256_digest,
)

from docspec.domain.source_catalog import (
    CatalogDisposition,
)
from docspec.domain.storage import partition_bucket
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    SourceCatalogSuccession,
)

CATALOG_KIND = "docspec-source-catalog"


CATALOG_POLICY_KEY = "catalog-policy.json"


CATALOG_RECEIPT_KEY = "catalog-build-receipt.json"


CATALOG_MANIFEST_KEY = "manifests/catalog.json"


CATALOG_POLICY_ROLE = "catalog-policy"


CATALOG_ITEMS_ROLE = "source-items"


CATALOG_RECEIPT_ROLE = "catalog-build-receipt"


CATALOG_ITEMS_MEDIA_TYPE = "application/x-ndjson"


CATALOG_JSON_MEDIA_TYPE = "application/json"


CATALOG_POLICY_FORMAT = "docspec-catalog-policy"


CATALOG_RECEIPT_FORMAT = "docspec-source-catalog-build-receipt"


CATALOG_FORMAT_VERSION = "1.0"


MAX_CATALOG_ROW_BYTES = 4 * 1024 * 1024


MAX_SMALL_MEMBER_BYTES = 1024 * 1024


MAX_SOURCE_RENDITIONS_PER_RECORD = 1024


MAX_SOURCE_RENDITION_BYTES_PER_RECORD = 4 * 1024 * 1024


CATALOG_PARTITION_POLICY_ID = "urn:docspec:partition-policy:source-item-sha256:1"


CATALOG_PARTITION_POLICY_VERSION = "1.0.0"


CATALOG_PARTITION_BUCKET_COUNT = 64


_UNIVERSE_ACCOUNTING_NAMESPACE = "docspec-internal/universe"


_OUTPUT_ACCOUNTING_NAMESPACE = "docspec-internal/output"


_OUTPUT_PARTITION_NAMESPACE_PREFIX = "docspec-internal/output-partition/"


_SOURCE_ROW_NAMESPACE_PREFIX = "docspec-internal/source-rows/"


# Each of these is an integrity fingerprint over a derived, in-memory per-row
# projection (see `_derive_catalog`'s and `_derive_catalog_parallel`'s
# `diagnostics` dict below) -- not a reference to a published member. No
# member with this content is declared anywhere in the distribution; verifying
# one means recomputing it from the published `source-items` partitions, not
# dereferencing a blob. Full rationale is on the matching properties in
# `source_catalog.py`'s `source_catalog_schemas()` receipt schema.
_DIAGNOSTIC_DIGEST_FIELDS = (
    "normalizedFieldsDigest",
    "joinedFieldsDigest",
    "dispositionsDigest",
    "reasonsDigest",
    "interpretationsDigest",
    "renditionChoicesDigest",
)


_INTERPRETATION_KINDS = (
    "exact-join",
    "normalization",
    "rendition-preference",
    "sampling",
    "selection",
    "topic-recovery",
)


_CATALOG_SPEC_FIELDS = {
    "catalogId",
    "catalogSchemaDigest",
    "sourceSystemSetDigest",
    "sourceNativeSchemaSetDigest",
    "selectionPolicyId",
    "selectionPolicyVersion",
    "selectionPolicyDigest",
    "requestedUniverseSetDigest",
    "selectedSourceSetDigest",
    "catalogStateDigest",
}


_SOURCE_RECORD_FIELDS = {
    "sourceRecordId",
    "scopeId",
    "schemaName",
    "schemaVersion",
    "schemaDigest",
    "record",
    "fieldDiagnostics",
}


_SOURCE_RENDITION_REQUIRED_FIELDS = {
    "sourceRecordId",
    "renditionId",
    "sourceField",
    "locator",
    "mediaType",
    "expectedSha256",
    "expectedByteSize",
}


def source_catalog_producer(
    *,
    implementation_id: str,
    verifier_id: str,
    verifier_version: str,
    verifier_implementation_id: str,
) -> Producer:
    """Validate standard immutable implementation identities at the outer edge."""

    return Producer.from_dict(
        {
            "product": "docspec",
            "implementationId": implementation_id,
            "verifierId": verifier_id,
            "verifierVersion": verifier_version,
            "verifierImplementationId": verifier_implementation_id,
        },
        path="source-catalog/producer",
    )


@dataclass(frozen=True, slots=True)
class _CatalogPartition:
    partition_id: str
    member: MemberDescriptor

    def to_receipt(self) -> dict[str, object]:
        if self.member.blob_ref is None or self.member.record_count is None:
            raise ValueError("source-item partitions require blobRef and recordCount")
        return {
            "partitionId": self.partition_id,
            "blobRef": self.member.blob_ref,
            "byteSize": self.member.byte_size,
            "recordCount": self.member.record_count,
        }


def _partition_policy() -> dict[str, object]:
    identity = {
        "policyId": CATALOG_PARTITION_POLICY_ID,
        "policyVersion": CATALOG_PARTITION_POLICY_VERSION,
        "bucketCount": CATALOG_PARTITION_BUCKET_COUNT,
    }
    return {**identity, "policyDigest": sha256_digest(canonical_json_bytes(identity))}


def _partition_id(source_item_id: str) -> str:
    bucket = partition_bucket(source_item_id, CATALOG_PARTITION_BUCKET_COUNT)
    return f"{bucket:04d}"


def _partition_namespace(partition_id: str) -> str:
    return f"{_OUTPUT_PARTITION_NAMESPACE_PREFIX}{partition_id}"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{label} must be nonempty text")
    return value


def _utf16_key(value: str) -> bytes:
    """Use the shared artifact ordering rule for DocSpec-owned row keys."""

    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as error:
        raise IntegrityError("catalog identity contains a lone Unicode surrogate") from error


_SELECTED_DISPOSITION = CatalogDisposition.SELECTED.value


def _source_catalog_succession(value: object) -> SourceCatalogSuccession:
    supersedes = Supersedes.from_dict(value, path="source-catalog/supersedes")
    return SourceCatalogSuccession(
        supersedes.logical_id,
        supersedes.artifact_digest,
        supersedes.reason,
    )
