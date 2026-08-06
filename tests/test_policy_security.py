"""Closed policy, field-projection, and credential-safety checks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from docspec.application.execution import StoreExecutionService
from docspec.domain.policies import (
    DataUsePolicy,
    ProcessorExecutionScope,
    ProviderEvidence,
    ProviderEvidenceMode,
    ProviderInteractionEvidence,
    RetentionAction,
    RetentionPolicy,
)
from docspec.domain.processors import ProcessorPayload, ProcessorResourceUse
from docspec.domain.security import REDACTED_SECRET, redact, redact_text, require_secret_free
from docspec.errors import IntegrityError
from docspec.processing.extraction import TextExtractor
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import ParagraphSegmenter
from tests.helpers import processor_payload, segment_processor_request
from tests.test_processing_pipeline import _captured


def _segment(content: bytes = b"policy-projected content"):
    extraction = TextExtractor().extract(_captured(content, "text/plain"), content)
    return ParagraphSegmenter().segment(extraction.payload)[0]


def test_retention_and_data_use_policies_are_closed_versioned_and_identity_bearing() -> None:
    retention = RetentionPolicy.create(
        derived_records=RetentionAction.COLLECT_WHEN_UNREFERENCED,
        minimum_age_seconds=3600,
    )
    data_use = DataUsePolicy.create(
        execution_scope=ProcessorExecutionScope.DECLARED_EXTERNAL,
        allowed_fields=("content", "evidence"),
        request_evidence=ProviderEvidenceMode.REDACTED_RECORD,
        response_evidence=ProviderEvidenceMode.DIGEST_ONLY,
    )

    assert RetentionPolicy.from_dict(retention.to_dict()) == retention
    assert DataUsePolicy.from_dict(data_use.to_dict()) == data_use
    assert data_use.allows_external_processing

    for value, reader in (
        (retention.to_dict(), RetentionPolicy.from_dict),
        (data_use.to_dict(), DataUsePolicy.from_dict),
    ):
        extra = dict(value, unexpected=True)
        with pytest.raises(ValueError, match="closed shape"):
            reader(extra)
        changed = dict(value, policyId="urn:docspec:changed")
        with pytest.raises(ValueError, match="identity differs"):
            reader(changed)

    invalid_data_use = data_use.to_dict()
    invalid_data_use["allowedFields"] = ["content", 1]
    with pytest.raises((TypeError, ValueError), match="allowed field"):
        DataUsePolicy.from_dict(invalid_data_use)


def test_retry_policy_rejects_boolean_and_inverted_numeric_bounds() -> None:
    from docspec.domain.policies import RetryPolicy

    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=True)
    with pytest.raises(ValueError, match="retry delays"):
        RetryPolicy(base_delay_milliseconds=10, max_delay_milliseconds=9)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter_basis_points=False)


def test_processor_payload_contains_only_policy_selected_fields() -> None:
    segment = _segment()
    policy = DataUsePolicy.create(
        execution_scope=ProcessorExecutionScope.DECLARED_EXTERNAL,
        allowed_fields=("content", "evidence"),
    )

    payload = ProcessorPayload.for_segment(segment.segment, segment.content, policy.allowed_fields)

    assert payload.content == segment.content
    assert payload.evidence == segment.segment.evidence
    assert payload.content_media_type is None
    assert payload.representation_coordinates is None
    assert payload.segment_kind is None
    assert payload.segment_ordinal is None
    with pytest.raises(IntegrityError, match="policy excludes"):
        payload.require("representationCoordinates")

    metadata_only = DataUsePolicy.create(
        execution_scope=ProcessorExecutionScope.LOCAL_ONLY,
        allowed_fields=("evidence",),
    )
    metadata_payload = ProcessorPayload.for_segment(
        segment.segment,
        segment.content,
        metadata_only.allowed_fields,
    )
    assert metadata_payload.input_byte_size == 0
    assert (
        StoreExecutionService._projected_segment_byte_size(
            segment.segment,
            metadata_only.allowed_fields,
        )
        == 0
    )
    with pytest.raises(IntegrityError, match="immutable segment reference"):
        ProcessorPayload.for_segment(segment.segment, segment.content + b"changed", policy.allowed_fields)


def test_external_provider_evidence_matches_the_sealed_data_use_modes() -> None:
    policy = DataUsePolicy.create(
        execution_scope=ProcessorExecutionScope.DECLARED_EXTERNAL,
        allowed_fields=("content",),
        request_evidence=ProviderEvidenceMode.REDACTED_RECORD,
        response_evidence=ProviderEvidenceMode.DIGEST_ONLY,
    )
    evidence = ProviderInteractionEvidence(
        "urn:docspec:test:provider",
        ProviderEvidence.redacted({"requestId": "provider-request-1", "fieldNames": ["content"]}),
        ProviderEvidence.digest_only("sha256:" + "a" * 64),
    )

    policy.require_provider_evidence(evidence, external=True)
    with pytest.raises(ValueError, match="request evidence differs"):
        policy.require_provider_evidence(
            ProviderInteractionEvidence(
                evidence.provider_id,
                ProviderEvidence.digest_only(evidence.request.digest),
                evidence.response,
            ),
            external=True,
        )
    with pytest.raises(ValueError, match="must not attach"):
        policy.require_provider_evidence(evidence, external=False)


def test_external_request_accounting_matches_the_declared_execution_scope() -> None:
    processor = ContentStatisticsProcessor()
    segment = _segment()
    request = segment_processor_request(processor, segment)
    result = processor.process(
        request,
        processor_payload(segment),
        (),
    )

    with pytest.raises(IntegrityError, match="external-request count"):
        StoreExecutionService._validate_processor_result(
            replace(
                result,
                resource_use=ProcessorResourceUse(
                    result.resource_use.input_bytes,
                    result.resource_use.output_bytes,
                    result.resource_use.duration_milliseconds,
                    external_request_count=1,
                ),
            ),
            request,
            processor.description,
            segment.segment,
            len(segment.content),
            (),
            data_use_policy=DataUsePolicy.local_content(),
            require_current_request=True,
        )


def test_processor_receipts_reject_secrets_and_diagnostics_redact_them() -> None:
    segment = _segment()
    processor = ContentStatisticsProcessor()
    result = processor.process(
        segment_processor_request(processor, segment),
        processor_payload(segment),
        (),
    )

    with pytest.raises(IntegrityError, match="secret-like content"):
        replace(result, provider_receipt={"authorization": "ordinary-looking-value"})
    with pytest.raises(IntegrityError, match="secret-like content"):
        replace(result, warnings=("password=correct-horse-battery-staple",))
    with pytest.raises(IntegrityError, match="secret-like content"):
        require_secret_free({"message": "Bearer abcdefghijklmnopqrstuvwxyz"}, label="fixture")

    assert redact_text("failure: password=correct-horse-battery-staple") == REDACTED_SECRET
    assert redact(
        {
            "apiKey": "ordinary-looking-value",
            "message": "Bearer abcdefghijklmnopqrstuvwxyz",
            "safe": "retained",
        }
    ) == {
        "apiKey": REDACTED_SECRET,
        "message": REDACTED_SECRET,
        "safe": "retained",
    }


@pytest.mark.parametrize(
    "secret",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "sk" + "_live_" + "abcdefghijklmnopqrstuvwxyz",
        "eyJabcdefghi.abcdefghijklmnop.abcdefghijklmnop",
        "aws_secret_access_key=abcdefghijklmnopqrstuvwxyz1234567890ABCD",
        "-----BEGIN PRIVATE KEY-----",
    ),
)
def test_common_provider_credentials_are_detected_as_defense_in_depth(secret: str) -> None:
    with pytest.raises(IntegrityError, match="secret-like content"):
        require_secret_free({"diagnostic": secret}, label="fixture")
