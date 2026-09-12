"""Stage policy output, account for bytes, verify, and publish one catalog."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from rulespec_artifacts import (
    ROOT_OBJECT_KEY,
    ArtifactInput,
    ArtifactPin,
    ArtifactVerificationError,
    MemberManifestReference,
    Producer,
    Supersedes,
    admit_artifact,
    build_artifact_root,
    canonical_json_bytes,
    describe_member_from_receipt,
    schema_bundle_digest,
    sha256_digest,
)

from docspec.adapters.catalog_artifact import derivation
from docspec.adapters.catalog_artifact.accounting import _DispositionTally
from docspec.adapters.catalog_artifact.digests import (
    _DerivedCatalog,
    _source_schema_set_digest,
    _source_system_set_digest,
)
from docspec.adapters.catalog_artifact.inputs import _policy_rows, _ResumeLedger
from docspec.adapters.catalog_artifact.rows import _require_interpretation_order
from docspec.adapters.catalog_artifact.rules import (
    _OUTPUT_ACCOUNTING_NAMESPACE,
    CATALOG_FORMAT_VERSION,
    CATALOG_RECEIPT_FORMAT_VERSION,
    CATALOG_ITEMS_MEDIA_TYPE,
    CATALOG_ITEMS_ROLE,
    CATALOG_JSON_MEDIA_TYPE,
    CATALOG_KIND,
    CATALOG_MANIFEST_KEY,
    CATALOG_POLICY_FORMAT,
    CATALOG_POLICY_KEY,
    CATALOG_POLICY_ROLE,
    CATALOG_RECEIPT_FORMAT,
    CATALOG_RECEIPT_KEY,
    CATALOG_RECEIPT_ROLE,
    MAX_CATALOG_ROW_BYTES,
    MAX_SMALL_MEMBER_BYTES,
    _CatalogPartition,
    _partition_id,
    _partition_namespace,
    _partition_policy,
    _utf16_key,
)
from docspec.adapters.catalog_artifact.schemas import (
    _ITEM_VALIDATOR,
    _POLICY_VALIDATOR,
    _RECEIPT_VALIDATOR,
    _SCHEMAS,
    _schema_error,
)
from docspec.adapters.catalog_artifact.verification import SourceCatalogBuildGateVerifier
from docspec.domain.identity import require_text
from docspec.domain.source_outcomes import (
    DEFAULT_ACCEPTED_RECORD_OUTCOMES, accepted_record_outcomes, require_accepted_outcome,
)
from docspec.domain.references import SourceCatalogRef
from docspec.domain.source_catalog import (
    SOURCE_CATALOG_ITEM_SCHEMA_ID,
    SOURCE_CATALOG_POLICY_SCHEMA_ID,
    SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
    CatalogDisposition,
    SourceCatalogItem,
)
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import (
    CatalogPolicyWorkspace,
    SourceCatalogPolicy,
    SourceCatalogSnapshotSummary,
    SourceCatalogStaging,
    SourceCatalogStore,
    SourceNativeDescription,
    SourceNativeRecordSource,
)


@dataclass(frozen=True, slots=True)
class SourceCatalogBuildRequest:
    catalog_id: str
    producer: Producer
    supersedes: Supersedes | None = None
    accepted_record_outcomes: frozenset[str] = DEFAULT_ACCEPTED_RECORD_OUTCOMES

    def __post_init__(self) -> None:
        require_text(self.catalog_id, "source catalog series catalog_id")
        object.__setattr__(self, "accepted_record_outcomes", accepted_record_outcomes(self.accepted_record_outcomes))
        if self.supersedes is not None:
            if not isinstance(self.supersedes, Supersedes):
                raise TypeError("source catalog supersedes must use Rulespec Supersedes")
            Supersedes.from_dict(self.supersedes.as_dict(), path="source-catalog/supersedes")
            require_text(self.supersedes.reason, "source catalog supersedes reason")


@dataclass(frozen=True, slots=True)
class _DescribedSource:
    source: SourceNativeRecordSource
    description: SourceNativeDescription

    def describe(self) -> SourceNativeDescription:
        return self.description

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        yield from self.source.iter_records()

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
        yield from self.source.iter_renditions()


def _snapshot_sources(
    sources: Sequence[SourceNativeRecordSource], accepted: frozenset[str],
) -> tuple[SourceNativeRecordSource, ...]:
    """Share one immutable description between preflight and publication."""

    if not sources:
        raise ValueError("a source catalog requires at least one source-native input")
    accepted = accepted_record_outcomes(accepted)
    result: list[SourceNativeRecordSource] = []
    description_bytes = 0
    for source in sources:
        description = source.describe()
        if not isinstance(description, SourceNativeDescription):
            raise TypeError("source describe() must return SourceNativeDescription")
        require_accepted_outcome(description.collection_outcome, accepted)
        description_bytes += len(canonical_json_bytes(description.to_dict()))
        if description_bytes > MAX_SMALL_MEMBER_BYTES:
            raise LimitExceededError("source descriptions exceed the catalog metadata byte limit")
        result.append(source if isinstance(source, _DescribedSource) else _DescribedSource(source, description))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SourceCatalogBuildResult:
    reference: SourceCatalogRef
    summary: SourceCatalogSnapshotSummary
    byte_measurements: Mapping[str, int]
    #: Which engine derived the digests, for the build and for the producer
    #: gate's recomputation: ``{"build": {...}, "gate": {...}}``, each a
    #: ``DERIVATION_PATHS`` member with its worker count. The parallel engine
    #: falls back to the serial one silently when workers cannot start, and
    #: nothing else records which path a receipt's digests came from.
    derivation: Mapping[str, Mapping[str, object]]


class _CatalogRowPartitioner:
    def __init__(
        self,
        rows: Iterable[SourceCatalogItem],
        *,
        ledger: _ResumeLedger,
        batch_items: int,
    ) -> None:
        if batch_items < 1:
            raise ValueError("resume batch size must be at least one item")
        self._rows = rows
        self._ledger = ledger
        self._batch_items = batch_items
        self.item_count = 0
        self.tally = _DispositionTally()
        self.disposition_counts = self.tally.dispositions
        self.selected_count = 0
        self.partition_counts: dict[str, int] = {}
        self.last_item_id: str | None = None

    def state(self) -> dict[str, Any]:
        """Everything a resumed run restores instead of recomputing.

        The producer gate re-derives all of it from the staged rows before
        publication, so a stale or tampered state fails closed there.
        """

        return {
            "after": self.last_item_id,
            "itemCount": self.item_count,
            "selectedCount": self.selected_count,
            "partitionCounts": dict(self.partition_counts),
            **self.tally.to_state(),
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.last_item_id = None if state["after"] is None else str(state["after"])
        self.item_count = int(state["itemCount"])
        self.selected_count = int(state["selectedCount"])
        self.partition_counts = {str(k): int(v) for k, v in state["partitionCounts"].items()}
        self.tally = _DispositionTally.from_state(state)
        self.disposition_counts = self.tally.dispositions

    def stage(self, workspace: CatalogPolicyWorkspace) -> None:
        previous: str | None = self.last_item_id
        started = False
        for item in self._rows:
            if not started:
                started = True
                # The policy's pre-pass is complete once it yields; commit it
                # on its own so a kill during the first batch resumes here.
                if not self._ledger.point.indexed:
                    self._ledger.mark_indexed()
            if previous is not None and _utf16_key(item.source_item_id) <= _utf16_key(previous):
                raise IntegrityError("catalog policy produced duplicate or out-of-order sourceItemId values")
            previous = item.source_item_id
            value = item.to_dict()
            _ITEM_VALIDATOR.error(value, f"source-catalog row {self.item_count}")
            _require_interpretation_order(value)
            payload = canonical_json_bytes(value)
            if len(payload) > MAX_CATALOG_ROW_BYTES:
                raise LimitExceededError("source-catalog row exceeds its byte limit")
            self.item_count += 1
            self.tally.add(item.disposition.value, item.selection.reason_code)
            if item.disposition is CatalogDisposition.SELECTED:
                self.selected_count += 1
            selected_partition = _partition_id(item.source_item_id)
            put_payload = getattr(workspace, "put_payload", None)
            if put_payload is not None:
                put_payload(
                    _partition_namespace(selected_partition),
                    (item.source_item_id,),
                    payload,
                )
            else:
                workspace.put(
                    _partition_namespace(selected_partition),
                    (item.source_item_id,),
                    value,
                )
            self.partition_counts[selected_partition] = self.partition_counts.get(selected_partition, 0) + 1
            # Accounting sits beside the payload so every commit holds both
            # for exactly the same items; a kill can never leave them apart.
            workspace.put(
                _OUTPUT_ACCOUNTING_NAMESPACE,
                (item.source_item_id,),
                {"sourceItemId": item.source_item_id},
            )
            self.last_item_id = item.source_item_id
            if self.item_count % self._batch_items == 0:
                self._ledger.mark_cursor(self.state())

    @staticmethod
    def chunks(workspace: CatalogPolicyWorkspace, partition_id: str) -> Iterator[bytes]:
        iter_payloads = getattr(workspace, "iter_payloads", None)
        if iter_payloads is not None:
            # The workspace stores exactly the canonical bytes stage() produced;
            # streaming them verbatim avoids a parse and a re-serialization per
            # row per read, and the artifact's own gates re-verify every row.
            for payload in iter_payloads(_partition_namespace(partition_id)):
                yield payload + b"\n"
            return
        for value in workspace.iter_ordered(_partition_namespace(partition_id)):
            yield canonical_json_bytes(value) + b"\n"


def _measure_blob(chunks: Iterable[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("source-catalog blob measurements require bytes")
        digest.update(chunk)
        byte_size += len(chunk)
    return "sha256:" + digest.hexdigest(), byte_size


class SourceCatalogBuilder:
    """Create one complete snapshot from injected source, policy, and storage ports."""

    def __init__(
        self,
        *,
        store: SourceCatalogStore,
        policy: SourceCatalogPolicy,
        request: SourceCatalogBuildRequest,
        workspace_factory: Callable[
            [],
            AbstractContextManager[CatalogPolicyWorkspace],
        ],
        resume_batch_items: int = 10_000,
    ) -> None:
        self._store = store
        self._policy = policy
        self._request = request
        self._resume_batch_items = resume_batch_items
        self._workspace_factory = workspace_factory

    def build(self, sources: Sequence[SourceNativeRecordSource]) -> SourceCatalogBuildResult:
        sources = _snapshot_sources(sources, self._request.accepted_record_outcomes)
        descriptions = tuple(source.describe() for source in sources)
        policy = {
            "format": CATALOG_POLICY_FORMAT,
            "formatVersion": CATALOG_FORMAT_VERSION,
            "policyId": self._policy.policy_id,
            "policyVersion": self._policy.policy_version,
            "configuration": dict(self._policy.configuration),
        }
        _schema_error(_POLICY_VALIDATOR, policy, "catalog policy")
        policy_bytes = canonical_json_bytes(policy)
        policy_digest = sha256_digest(policy_bytes)
        catalog_schema_digest = schema_bundle_digest(_SCHEMAS)

        with self._workspace_factory() as workspace, self._store.stage() as staging:
            row_partitioner = self._stage_policy_rows(
                sources, descriptions, workspace, policy_digest, catalog_schema_digest
            )
            partitions, payload_bytes_reused, payload_bytes_written = self._stage_partition_blobs(
                row_partitioner, workspace, staging
            )
            selected_blob_source = staging.blob_source()
            # The builder reads back bytes it staged itself; the producer gate
            # independently re-validates every row before publication, so this
            # derivation skips the redundant schema pass.
            derived = derivation._derive_catalog(
                selected_blob_source,
                partitions,
                item_count=row_partitioner.item_count,
                selected_count=row_partitioner.selected_count,
                validate_rows=False,
            )
            build_derivation = dict(derived.derivation)
            spec, ordered_inputs, receipt = self._publication_metadata(
                descriptions,
                catalog_schema_digest,
                policy_digest,
                derived,
                row_partitioner,
                partitions,
                payload_bytes_reused,
                payload_bytes_written,
            )
            reference = self._stage_publication(
                staging, policy_bytes, spec, ordered_inputs, receipt, partitions
            )
            verifier = SourceCatalogBuildGateVerifier(self._request.producer, selected_blob_source)
            try:
                admit_artifact(
                    staging,
                    blob_source=selected_blob_source,
                    expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                    semantic_verifier=verifier,
                )
            except ArtifactVerificationError as error:
                raise IntegrityError(f"built source catalog is structurally invalid: {error}") from error
            published = staging.commit(reference)
        if verifier.summary is None:
            raise RuntimeError("source catalog verifier produced no summary")
        return SourceCatalogBuildResult(
            published,
            verifier.summary,
            dict(receipt["byteMeasurements"]),
            {"build": build_derivation, "gate": dict(verifier.derivation)},
        )

    def _stage_policy_rows(
        self,
        sources: Sequence[SourceNativeRecordSource],
        descriptions: tuple[SourceNativeDescription, ...],
        workspace: CatalogPolicyWorkspace,
        policy_digest: str,
        catalog_schema_digest: str,
    ) -> _CatalogRowPartitioner:
        """Restore or complete the durable policy output and its accounting."""

        ledger = _ResumeLedger(workspace)
        ledger.open(
            {
                "catalogId": self._request.catalog_id,
                "catalogSchemaDigest": catalog_schema_digest,
                "policyDigest": policy_digest,
                "producer": self._request.producer.as_dict(),
                "acceptedRecordOutcomes": sorted(self._request.accepted_record_outcomes),
                "inputs": [value.to_dict() for value in descriptions],
            }
        )
        row_partitioner = _CatalogRowPartitioner(
            _policy_rows(
                sources,
                descriptions,
                self._policy,
                policy_digest,
                workspace,
                ledger,
            ),
            ledger=ledger,
            batch_items=self._resume_batch_items,
        )
        if ledger.staged_state is not None:
            # Every row is staged and accounted; only publication remains.
            row_partitioner.restore(ledger.staged_state)
        else:
            if ledger.cursor_state is not None:
                row_partitioner.restore(ledger.cursor_state)
            row_partitioner.stage(workspace)
            ledger.mark_staged(row_partitioner.state())
        return row_partitioner

    @staticmethod
    def _stage_partition_blobs(
        row_partitioner: _CatalogRowPartitioner,
        workspace: CatalogPolicyWorkspace,
        staging: SourceCatalogStaging,
    ) -> tuple[list[_CatalogPartition], int, int]:
        """Write ordered payloads and return partitions, reused bytes, and written bytes."""

        partitions: list[_CatalogPartition] = []
        payload_bytes_reused = 0
        payload_bytes_written = 0
        for partition_id in sorted(row_partitioner.partition_counts, key=_utf16_key):
            blob_ref, byte_size = _measure_blob(_CatalogRowPartitioner.chunks(workspace, partition_id))
            write = staging.put_blob(
                blob_ref,
                byte_size,
                _CatalogRowPartitioner.chunks(workspace, partition_id),
            )
            if write.reused:
                payload_bytes_reused += write.byte_size
            else:
                payload_bytes_written += write.byte_size
            partitions.append(
                _CatalogPartition(
                    partition_id,
                    describe_member_from_receipt(
                        blob_ref=write.blob_ref,
                        role=CATALOG_ITEMS_ROLE,
                        media_type=CATALOG_ITEMS_MEDIA_TYPE,
                        byte_size=write.byte_size,
                        record_count=row_partitioner.partition_counts[partition_id],
                        schema_id=SOURCE_CATALOG_ITEM_SCHEMA_ID,
                    ),
                )
            )
        return partitions, payload_bytes_reused, payload_bytes_written

    def _publication_metadata(
        self,
        descriptions: tuple[SourceNativeDescription, ...],
        catalog_schema_digest: str,
        policy_digest: str,
        derived: _DerivedCatalog,
        row_partitioner: _CatalogRowPartitioner,
        partitions: Sequence[_CatalogPartition],
        payload_bytes_reused: int,
        payload_bytes_written: int,
    ) -> tuple[dict[str, Any], tuple[ArtifactInput, ...], dict[str, Any]]:
        """Describe the derived catalog and its accounted inputs before sealing."""

        state_digest = derived.catalog_state_digest
        requested_digest = derived.requested_universe_set_digest
        selected_digest = derived.selected_source_set_digest
        diagnostics = derived.diagnostics
        spec = {
            "catalogId": self._request.catalog_id,
            "catalogSchemaDigest": catalog_schema_digest,
            "sourceSystemSetDigest": _source_system_set_digest(descriptions),
            "sourceNativeSchemaSetDigest": _source_schema_set_digest(descriptions),
            "selectionPolicyId": self._policy.policy_id,
            "selectionPolicyVersion": self._policy.policy_version,
            "selectionPolicyDigest": policy_digest,
            "requestedUniverseSetDigest": requested_digest,
            "selectedSourceSetDigest": selected_digest,
            "catalogStateDigest": state_digest,
        }
        inputs = tuple(
            ArtifactInput("source-native", value.logical_id, value.artifact_digest) for value in descriptions
        )
        ordered_inputs = tuple(
            sorted(
                inputs,
                key=lambda value: _utf16_key(value.logical_id.rsplit(":", 1)[-1]),
            )
        )
        payload_bytes_read = payload_bytes_reused + payload_bytes_written
        by_pin = {(value.logical_id, value.artifact_digest): value for value in descriptions}
        receipt: dict[str, Any] = {
            "format": CATALOG_RECEIPT_FORMAT,
            "formatVersion": CATALOG_RECEIPT_FORMAT_VERSION,
            "catalogId": self._request.catalog_id,
            "catalogSchemaDigest": catalog_schema_digest,
            "sourceSystemSetDigest": spec["sourceSystemSetDigest"],
            "sourceNativeSchemaSetDigest": spec["sourceNativeSchemaSetDigest"],
            "selectionPolicyId": self._policy.policy_id,
            "selectionPolicyVersion": self._policy.policy_version,
            "selectionPolicyDigest": policy_digest,
            "acceptedRecordOutcomes": sorted(self._request.accepted_record_outcomes),
            "sourceNativeInputs": [by_pin[(value.logical_id, value.artifact_digest)].to_dict() for value in ordered_inputs],
            "catalogStateDigest": state_digest,
            "requestedUniverseSetDigest": requested_digest,
            "selectedSourceSetDigest": selected_digest,
            "itemCount": row_partitioner.item_count,
            "dispositionCounts": row_partitioner.disposition_counts,
            "reasonCounts": row_partitioner.tally.reason_counts(),
            "partitionPolicy": _partition_policy(),
            "partitions": [value.to_receipt() for value in partitions],
            **diagnostics,
            "byteMeasurements": {
                "payloadBytesRead": payload_bytes_read,
                "payloadBytesReused": payload_bytes_reused,
                "payloadBytesWritten": payload_bytes_written,
                "publicationBytesWritten": 0,
            },
            "verifierId": self._request.producer.verifier_id,
            "verifierVersion": self._request.producer.verifier_version,
            "verifierImplementationId": self._request.producer.verifier_implementation_id,
            "semanticVerdict": "pass",
        }
        return spec, ordered_inputs, receipt

    def _stage_publication(
        self,
        staging: SourceCatalogStaging,
        policy_bytes: bytes,
        spec: Mapping[str, Any],
        ordered_inputs: tuple[ArtifactInput, ...],
        receipt: dict[str, Any],
        partitions: Sequence[_CatalogPartition],
    ) -> SourceCatalogRef:
        """Stabilize publication byte accounting, then write the unpublished members."""

        publication_bytes = -1
        for _ in range(8):
            receipt["byteMeasurements"]["publicationBytesWritten"] = publication_bytes
            receipt_bytes = canonical_json_bytes(receipt)
            local_members = (
                describe_member_from_receipt(
                    object_key=CATALOG_POLICY_KEY,
                    sha256=sha256_digest(policy_bytes),
                    role=CATALOG_POLICY_ROLE,
                    media_type=CATALOG_JSON_MEDIA_TYPE,
                    byte_size=len(policy_bytes),
                    schema_id=SOURCE_CATALOG_POLICY_SCHEMA_ID,
                ),
                describe_member_from_receipt(
                    object_key=CATALOG_RECEIPT_KEY,
                    sha256=sha256_digest(receipt_bytes),
                    role=CATALOG_RECEIPT_ROLE,
                    media_type=CATALOG_JSON_MEDIA_TYPE,
                    byte_size=len(receipt_bytes),
                    schema_id=SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
                ),
            )
            members = (*local_members, *(value.member for value in partitions))
            manifest, manifest_bytes = MemberManifestReference.for_members(
                scope_kind="global",
                scope_id="catalog",
                object_key=CATALOG_MANIFEST_KEY,
                members=members,
            )
            root = build_artifact_root(
                kind=CATALOG_KIND,
                spec=spec,
                producer=self._request.producer,
                inputs=ordered_inputs,
                manifests=(manifest,),
                supersedes=self._request.supersedes,
            )
            root_bytes = canonical_json_bytes(root)
            measured_publication_bytes = (
                len(policy_bytes) + len(receipt_bytes) + len(manifest_bytes) + len(root_bytes)
            )
            if measured_publication_bytes == publication_bytes:
                break
            publication_bytes = measured_publication_bytes
        else:
            raise IntegrityError("catalog publication byte accounting did not stabilize")
        _schema_error(_RECEIPT_VALIDATOR, receipt, "catalog build receipt")
        if len(receipt_bytes) > MAX_SMALL_MEMBER_BYTES:
            raise LimitExceededError("catalog build receipt exceeds its metadata byte limit")
        staging.write(CATALOG_POLICY_KEY, (policy_bytes,))
        staging.write(CATALOG_RECEIPT_KEY, (receipt_bytes,))
        staging.write(CATALOG_MANIFEST_KEY, (manifest_bytes,))
        staging.write(ROOT_OBJECT_KEY, (root_bytes,))
        reference = SourceCatalogRef(
            root["logicalId"],
            f"{root['artifactDigest'].removeprefix('sha256:')}/{ROOT_OBJECT_KEY}",
            root["artifactDigest"],
        )
        return reference
