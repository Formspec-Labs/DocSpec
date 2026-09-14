"""Check source-catalog meaning and independently rederive sealed receipts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rulespec_artifacts import (
    ROOT_OBJECT_KEY,
    MemberDescriptor,
    MemberSource,
    Producer,
    VerifiedArtifact,
    canonical_json_bytes,
    iter_member_descriptors,
    parse_canonical_json,
    schema_bundle_digest,
    sha256_digest,
)

from docspec.adapters.catalog_artifact import derivation
from docspec.adapters.catalog_artifact.accounting import _reconcile_reason_counts
from docspec.adapters.catalog_artifact.digests import _source_schema_set_digest, _source_system_set_digest
from docspec.adapters.catalog_artifact.rows import _read_small
from docspec.adapters.catalog_artifact.rules import (
    _CATALOG_SPEC_FIELDS,
    _DIAGNOSTIC_DIGEST_FIELDS,
    CATALOG_ITEMS_MEDIA_TYPE,
    CATALOG_ITEMS_ROLE,
    CATALOG_JSON_MEDIA_TYPE,
    CATALOG_KIND,
    CATALOG_PARTITION_BUCKET_COUNT,
    CATALOG_POLICY_KEY,
    CATALOG_POLICY_ROLE,
    CATALOG_RECEIPT_KEY,
    CATALOG_RECEIPT_ROLE,
    _CatalogPartition,
    _mapping,
    _partition_policy,
    _source_catalog_succession,
    _text,
    _utf16_key,
)
from docspec.adapters.catalog_artifact.schemas import _POLICY_VALIDATOR, _RECEIPT_VALIDATOR, _SCHEMAS
from docspec.adapters.schema_validation import validate_payload
from docspec.domain.source_catalog import (
    SOURCE_CATALOG_ITEM_SCHEMA_ID,
    SOURCE_CATALOG_POLICY_SCHEMA_ID,
    SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
    CatalogDisposition,
)
from docspec.errors import IntegrityError
from docspec.domain.source_outcomes import accepted_record_outcomes, require_accepted_outcome
from docspec.ports.source_catalog import (
    SourceCatalogBlobSource,
    SourceCatalogSnapshotSummary,
    SourceNativeDescription,
)


def _read_catalog_member(source: MemberSource, member: MemberDescriptor) -> bytes:
    """Bind each small product member to the manifest already checked."""

    assert member.object_key is not None
    payload = _read_small(source, member.object_key)
    if len(payload) != member.byte_size or sha256_digest(payload) != member.sha256:
        raise IntegrityError(f"source catalog member changed after admission: {member.object_key}")
    return payload


class SourceCatalogArtifactVerifier:
    """Check DocSpec meaning after Rulespec has checked generic structure."""

    def __init__(self, producer: Producer, blob_source: SourceCatalogBlobSource) -> None:
        self._producer = producer
        self._blob_source = blob_source
        self.summary: SourceCatalogSnapshotSummary | None = None
        self.partitions: tuple[_CatalogPartition, ...] = ()
        self.receipt: Mapping[str, Any] | None = None

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        root = artifact.root
        if root["kind"] != CATALOG_KIND:
            raise IntegrityError("source catalog reference names a different product kind")
        spec = _mapping(root["spec"], "source-catalog spec")
        if set(spec) != _CATALOG_SPEC_FIELDS:
            raise IntegrityError("source-catalog spec has an invalid closed shape")
        if not artifact.inputs or {value.role for value in artifact.inputs} != {"source-native"}:
            raise IntegrityError("source catalog must pin one or more source-native inputs")
        declared_members = tuple(iter_member_descriptors(artifact, source))
        local_members = {value.object_key: value for value in declared_members if value.object_key is not None}
        item_members = tuple(value for value in declared_members if value.role == CATALOG_ITEMS_ROLE)
        if set(local_members) != {CATALOG_POLICY_KEY, CATALOG_RECEIPT_KEY}:
            raise IntegrityError("source-catalog members differ from the DocSpec product view")
        if len(declared_members) != len(local_members) + len(item_members):
            raise IntegrityError("source-catalog member roles differ from the closed product role set")
        policy_member = local_members[CATALOG_POLICY_KEY]
        receipt_member = local_members[CATALOG_RECEIPT_KEY]
        if (
            policy_member.role != CATALOG_POLICY_ROLE
            or policy_member.media_type != CATALOG_JSON_MEDIA_TYPE
            or policy_member.schema_id != SOURCE_CATALOG_POLICY_SCHEMA_ID
            or policy_member.record_count is not None
            or receipt_member.role != CATALOG_RECEIPT_ROLE
            or receipt_member.media_type != CATALOG_JSON_MEDIA_TYPE
            or receipt_member.schema_id != SOURCE_CATALOG_RECEIPT_SCHEMA_ID
            or receipt_member.record_count is not None
        ):
            raise IntegrityError("source-catalog member descriptions are invalid")
        policy = parse_canonical_json(_read_catalog_member(source, policy_member), path=CATALOG_POLICY_KEY)
        receipt = parse_canonical_json(_read_catalog_member(source, receipt_member), path=CATALOG_RECEIPT_KEY)
        validate_payload(_POLICY_VALIDATOR, policy, "catalog policy")
        validate_payload(_RECEIPT_VALIDATOR, receipt, "catalog build receipt")
        policy = _mapping(policy, "catalog policy")
        receipt = _mapping(receipt, "catalog build receipt")
        producer = _mapping(root["producer"], "source-catalog producer")
        expected_producer = self._producer.as_dict()
        if producer != expected_producer:
            raise IntegrityError("source-catalog producer differs from the installed implementation")
        comparisons = {
            "catalogId": "catalogId",
            "catalogSchemaDigest": "catalogSchemaDigest",
            "sourceSystemSetDigest": "sourceSystemSetDigest",
            "sourceNativeSchemaSetDigest": "sourceNativeSchemaSetDigest",
            "selectionPolicyId": "selectionPolicyId",
            "selectionPolicyVersion": "selectionPolicyVersion",
            "selectionPolicyDigest": "selectionPolicyDigest",
            "catalogStateDigest": "catalogStateDigest",
            "requestedUniverseSetDigest": "requestedUniverseSetDigest",
            "selectedSourceSetDigest": "selectedSourceSetDigest",
        }
        for receipt_field, spec_field in comparisons.items():
            if receipt[receipt_field] != spec[spec_field]:
                raise IntegrityError(f"catalog build receipt {receipt_field} differs from the root")
        if spec["catalogSchemaDigest"] != schema_bundle_digest(_SCHEMAS):
            raise IntegrityError("source catalog schema digest differs from the installed schema family")
        expected_inputs = [
            {"logicalId": value.logical_id, "artifactDigest": value.artifact_digest} for value in artifact.inputs
        ]
        try:
            descriptions = tuple(SourceNativeDescription.from_dict(value) for value in receipt["sourceNativeInputs"])
            accepted = accepted_record_outcomes(receipt["acceptedRecordOutcomes"])
            if receipt["acceptedRecordOutcomes"] != sorted(accepted):
                raise ValueError("accepted record outcomes must have canonical order")
            for description in descriptions:
                require_accepted_outcome(description.collection_outcome, accepted)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"catalog input description or acceptance is invalid: {error}") from error
        if [{"logicalId": value.logical_id, "artifactDigest": value.artifact_digest} for value in descriptions] != expected_inputs:
            raise IntegrityError("catalog build receipt source-native inputs differ from the root")
        if (
            _source_system_set_digest(descriptions) != spec["sourceSystemSetDigest"]
            or _source_schema_set_digest(descriptions) != spec["sourceNativeSchemaSetDigest"]
        ):
            raise IntegrityError("catalog source description digests differ from the root")
        if (
            policy["policyId"] != spec["selectionPolicyId"]
            or policy["policyVersion"] != spec["selectionPolicyVersion"]
            or sha256_digest(canonical_json_bytes(policy)) != spec["selectionPolicyDigest"]
        ):
            raise IntegrityError("catalog policy identity differs from the root")
        if (
            receipt["verifierId"] != producer["verifierId"]
            or receipt["verifierVersion"] != producer["verifierVersion"]
            or receipt["verifierImplementationId"] != producer["verifierImplementationId"]
        ):
            raise IntegrityError("catalog build receipt verifier differs from the root producer")
        if receipt["partitionPolicy"] != _partition_policy():
            raise IntegrityError("catalog build receipt partition policy differs from the installed policy")
        partition_rows = receipt["partitions"]
        partitions: list[_CatalogPartition] = []
        previous_partition: str | None = None
        members_by_ref = {value.blob_ref: value for value in item_members}
        if None in members_by_ref or len(members_by_ref) != len(item_members):
            raise IntegrityError("source-item members require distinct blobRef values")
        for raw_partition in partition_rows:
            partition = _mapping(raw_partition, "catalog receipt partition")
            partition_id = _text(partition["partitionId"], "catalog partitionId")
            if previous_partition is not None and _utf16_key(partition_id) <= _utf16_key(previous_partition):
                raise IntegrityError("catalog receipt partitions must be ordered and distinct")
            previous_partition = partition_id
            if (
                len(partition_id) != 4
                or not partition_id.isascii()
                or not partition_id.isdigit()
                or not 0 <= int(partition_id) < CATALOG_PARTITION_BUCKET_COUNT
            ):
                raise IntegrityError("catalog receipt partitionId is outside the installed policy")
            member = members_by_ref.get(partition["blobRef"])
            if member is None:
                raise IntegrityError("catalog receipt partition has no matching source-items member")
            if (
                member.media_type != CATALOG_ITEMS_MEDIA_TYPE
                or member.schema_id != SOURCE_CATALOG_ITEM_SCHEMA_ID
                or member.record_count != partition["recordCount"]
                or member.byte_size != partition["byteSize"]
                or member.object_key is not None
                or member.sha256 is not None
            ):
                raise IntegrityError("source-item partition descriptor differs from its build receipt")
            partitions.append(_CatalogPartition(partition_id, member))
        if {value.member.blob_ref for value in partitions} != set(members_by_ref):
            raise IntegrityError("catalog receipt does not account for every source-items member")
        if receipt["itemCount"] != sum(value.member.record_count or 0 for value in partitions):
            raise IntegrityError("catalog build receipt item count differs from its source-item partitions")
        counts = receipt["dispositionCounts"]
        if sum(counts.values()) != receipt["itemCount"]:
            raise IntegrityError("catalog build receipt dispositions do not account for every row")
        _reconcile_reason_counts(receipt["reasonCounts"], counts)
        measurements = receipt["byteMeasurements"]
        payload_bytes = sum(value.member.byte_size for value in partitions)
        if (
            measurements["payloadBytesRead"] != payload_bytes
            or measurements["payloadBytesReused"] + measurements["payloadBytesWritten"] != payload_bytes
        ):
            raise IntegrityError("catalog build receipt payload byte measurements do not reconcile")
        publication_bytes = (
            policy_member.byte_size
            + receipt_member.byte_size
            + sum(value.byte_size for value in artifact.manifests)
            + len(_read_small(source, ROOT_OBJECT_KEY))
        )
        if measurements["publicationBytesWritten"] != publication_bytes:
            raise IntegrityError("catalog build receipt publication byte measurements do not reconcile")
        previous_join: str | None = None
        for coverage in receipt["joinCoverage"]:
            join_id = coverage["joinId"]
            if previous_join is not None and _utf16_key(join_id) <= _utf16_key(previous_join):
                raise IntegrityError("catalog join coverage must be ordered and distinct")
            previous_join = join_id
            if coverage["eligible"] != coverage["matched"] + coverage["unmatched"]:
                raise IntegrityError("catalog join coverage eligible count does not reconcile")
            if coverage["eligible"] + coverage["nullResult"] > receipt["itemCount"]:
                raise IntegrityError("catalog join coverage exceeds the catalog population")
        self.partitions = tuple(partitions)
        self.receipt = receipt
        self.summary = SourceCatalogSnapshotSummary(
            logical_id=artifact.pin.logical_id,
            artifact_digest=artifact.pin.artifact_digest,
            catalog_id=spec["catalogId"],
            catalog_state_digest=spec["catalogStateDigest"],
            requested_universe_set_digest=spec["requestedUniverseSetDigest"],
            selected_source_set_digest=spec["selectedSourceSetDigest"],
            item_count=receipt["itemCount"],
            disposition_counts=dict(counts),
            reason_counts=tuple(dict(value) for value in receipt["reasonCounts"]),
            partitions=tuple(value.partition_id for value in partitions),
            selection_policy={
                "policyId": spec["selectionPolicyId"],
                "policyVersion": spec["selectionPolicyVersion"],
                "policyDigest": spec["selectionPolicyDigest"],
            },
            partition_policy=dict(receipt["partitionPolicy"]),
            join_coverage=tuple(dict(value) for value in receipt["joinCoverage"]),
            diagnostic_digests={name: receipt[name] for name in _DIAGNOSTIC_DIGEST_FIELDS},
            source_native_inputs=tuple(dict(value) for value in receipt["sourceNativeInputs"]),
            accepted_record_outcomes=accepted,
            byte_measurements=dict(receipt["byteMeasurements"]),
            succession=(None if "supersedes" not in root else _source_catalog_succession(root["supersedes"])),
        )


class SourceCatalogBuildGateVerifier:
    """Add the producer-only full semantic pass to bounded receipt checks."""

    def __init__(self, producer: Producer, blob_source: SourceCatalogBlobSource) -> None:
        self._producer = producer
        self._blob_source = blob_source
        self.summary: SourceCatalogSnapshotSummary | None = None
        self.derivation: Mapping[str, object] = {}

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        receipt_verifier = SourceCatalogArtifactVerifier(self._producer, self._blob_source)
        receipt_verifier(artifact, source)
        summary = receipt_verifier.summary
        receipt = receipt_verifier.receipt
        if summary is None or receipt is None:
            raise RuntimeError("source catalog receipt verifier produced no summary")

        derived = derivation._derive_catalog(
            self._blob_source,
            receipt_verifier.partitions,
            item_count=summary.item_count,
            selected_count=summary.disposition_counts[CatalogDisposition.SELECTED.value],
        )
        self.derivation = dict(derived.derivation)
        computed = {
            "catalogStateDigest": derived.catalog_state_digest,
            "requestedUniverseSetDigest": derived.requested_universe_set_digest,
            "selectedSourceSetDigest": derived.selected_source_set_digest,
        }
        expected = {
            "catalogStateDigest": summary.catalog_state_digest,
            "requestedUniverseSetDigest": summary.requested_universe_set_digest,
            "selectedSourceSetDigest": summary.selected_source_set_digest,
        }
        for name, digest in computed.items():
            if digest != expected[name]:
                raise IntegrityError(f"producer semantic gate recomputed a different {name}")
        if derived.disposition_counts != dict(summary.disposition_counts):
            raise IntegrityError("producer semantic gate recomputed different disposition counts")
        if derived.reason_counts != [dict(value) for value in summary.reason_counts]:
            raise IntegrityError("producer semantic gate recomputed different reason counts")
        for name, value in derived.diagnostics.items():
            if receipt[name] != value:
                raise IntegrityError(f"producer semantic gate recomputed a different {name}")
        self.summary = summary
