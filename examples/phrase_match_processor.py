"""An injected local phrase matcher, kept in examples rather than the library.

Matches use literal phrases, Unicode word boundaries, and original UTF-8 byte
offsets. All overlapping phrases are retained; no semantic classification occurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from time import monotonic

from docspec.domain.content import DerivedRecord, ProcessorDisposition
from docspec.domain.identity import canonical_json_bytes, identity_digest, parse_closed_json, sha256_digest, thaw_json
from docspec.domain.policies import DataUsePolicy, ProcessorExecutionScope, RetryPolicy
from docspec.domain.processors import (
    ProcessorCacheMode, ProcessorCachePolicy, ProcessorDescription, ProcessorInput, ProcessorItemLimits,
    ProcessorPayload, ProcessorRequest, ProcessorResourceIdentity, ProcessorResourceKind, ProcessorResourceUse,
    ProcessorResult, processor_receipt_digest,
)
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.artifacts import utf8_byte_offsets

MAX_RESOURCE_BYTES = 64 * 1024
MAX_TERMS, MAX_PHRASES, MAX_PHRASE_CHARACTERS, MAX_MATCHES = 64, 256, 128, 1024


def _terms(resource_bytes: bytes) -> tuple[tuple[str, str, str], ...]:
    value = thaw_json(parse_closed_json(resource_bytes, label="phrase vocabulary"))
    if not isinstance(value, dict) or set(value) != {"terms"} or not isinstance(value["terms"], list):
        raise ValueError("phrase vocabulary requires only a terms array")
    if not 1 <= len(value["terms"]) <= MAX_TERMS:
        raise LimitExceededError("phrase vocabulary exceeds its term-count bounds")
    identifiers, result = set(), []
    for term in value["terms"]:
        if not isinstance(term, dict) or set(term) != {"id", "label", "phrases"}:
            raise ValueError("vocabulary term requires id, label, and phrases")
        if any(not isinstance(term[key], str) or not term[key].strip() for key in ("id", "label")):
            raise ValueError("vocabulary term identity and label must be nonempty text")
        if term["id"] in identifiers:
            raise ValueError("vocabulary term identity is duplicated")
        identifiers.add(term["id"])
        phrases = term["phrases"]
        if not isinstance(phrases, list) or not phrases:
            raise ValueError("vocabulary term requires nonempty phrases")
        seen = set()
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > MAX_PHRASE_CHARACTERS:
                raise ValueError("vocabulary phrase must be bounded nonempty text")
            if phrase in seen:
                raise ValueError("vocabulary phrase is duplicated within a term")
            seen.add(phrase)
            result.append((term["id"], term["label"], phrase))
            if len(result) > MAX_PHRASES:
                raise LimitExceededError("phrase vocabulary exceeds its phrase-count bound")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True, init=False)
class PhraseMatchProcessor:
    """Find mentions using exact caller-pinned reference data, without a provider."""

    description: ProcessorDescription
    _patterns: tuple = field(repr=False)

    def __init__(
        self, resource: ProcessorResourceIdentity, resource_bytes: bytes, *, case_sensitive: bool = False,
        item_limits: ProcessorItemLimits | None = None, retry_policy: RetryPolicy | None = None,
    ) -> None:
        if resource.resource_kind is not ProcessorResourceKind.REFERENCE_DATA:
            raise ValueError("phrase matching requires a reference-data resource")
        if not isinstance(resource_bytes, bytes):
            raise TypeError("phrase vocabulary must be immutable bytes")
        if len(resource_bytes) > MAX_RESOURCE_BYTES:
            raise LimitExceededError("phrase vocabulary exceeds its byte bound")
        if sha256_digest(resource_bytes) != resource.identity_digest:
            raise IntegrityError("phrase vocabulary bytes differ from the resource pin")
        if type(case_sensitive) is not bool:
            raise ValueError("case_sensitive must be a boolean")
        patterns = tuple(
            (identifier, label, phrase, re.compile(re.escape(phrase), 0 if case_sensitive else re.IGNORECASE))
            for identifier, label, phrase in _terms(resource_bytes)
        )
        object.__setattr__(self, "_patterns", patterns)
        configuration = {
            "matching": "escaped-literal-python-unicode-ignorecase/v1", "caseSensitive": case_sensitive,
            "boundaries": "adjacent-characters-must-not-be-unicode-alphanumeric-or-underscore",
            "overlap": "all-phrases-all-starts", "ordering": "byte-start-end-term-id-phrase",
            "maxResourceBytes": MAX_RESOURCE_BYTES, "maxTerms": MAX_TERMS,
            "maxPhrases": MAX_PHRASES, "maxPhraseCharacters": MAX_PHRASE_CHARACTERS, "maxMatches": MAX_MATCHES,
        }
        object.__setattr__(self, "description", ProcessorDescription.create(
            name="example-phrase-matches", version="1.0", implementation_id="docspec.example.PhraseMatchProcessor/v1",
            accepted_inputs=(ProcessorInput("segment", ("docspec-segment/1",), ("text/*",)),),
            output_schema_id="docspec-example-phrase-matches/1", output_media_types=("application/json",),
            execution_scope=ProcessorExecutionScope.LOCAL_ONLY, external_resources=(resource,), dependencies=(),
            deterministic=True,
            cache_policy=ProcessorCachePolicy(ProcessorCacheMode.EXACT_INPUTS, "docspec-exact-processor-cache-key/1"),
            configuration_digest=identity_digest(configuration), data_use_policy_digest=DataUsePolicy.local_content().digest,
            item_limits=item_limits or ProcessorItemLimits(1, 64 * 1024, 1, 64 * 1024, 5),
            retry_policy_digest=(retry_policy or RetryPolicy()).digest,
            capabilities=("literal-phrase-matches", "source-evidence"),
        ))

    def process(self, request: ProcessorRequest, payload: ProcessorPayload,
                prerequisite_results: tuple[ProcessorResult, ...]) -> ProcessorResult:
        started = monotonic()
        description = self.description
        payload.require("content")
        payload.require("evidence")
        if payload.content is None or payload.evidence is None:
            raise IntegrityError("phrase matcher requires segment content and evidence")
        if (
            request.processor_id != description.processor_id
            or request.processor_description_digest != identity_digest(description.to_dict())
            or request.input_records != (payload.input_record,) or request.allowed_fields != payload.allowed_fields
            or request.item_limits != description.item_limits or request.prerequisite_results or prerequisite_results
        ):
            raise IntegrityError("phrase matcher request differs from its pinned invocation")
        if len(payload.content) > description.item_limits.max_input_bytes:
            raise LimitExceededError("phrase matcher input exceeds its byte bound")
        try:
            text = payload.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("phrase matcher requires UTF-8 text") from error
        offsets = utf8_byte_offsets(text)
        matches, match_bytes = [], 0
        for identifier, label, phrase, pattern in self._patterns:
            position = 0
            while found := pattern.search(text, position):
                start, end = found.span()
                position = start + 1
                if monotonic() - started > description.item_limits.max_duration_seconds:
                    raise LimitExceededError("phrase matcher exceeds its duration bound")
                if (start and (text[start - 1].isalnum() or text[start - 1] == "_")) or (
                    end < len(text) and (text[end].isalnum() or text[end] == "_")
                ):
                    continue
                match = {
                    "termId": identifier, "label": label, "phrase": phrase, "quote": text[start:end],
                    "segmentByteStart": offsets[start], "segmentByteEnd": offsets[end],
                }
                match_bytes += len(canonical_json_bytes(match))
                if len(matches) >= MAX_MATCHES or match_bytes > description.item_limits.max_output_bytes:
                    raise LimitExceededError("phrase matches exceed their output bound")
                matches.append(match)
        matches.sort(key=lambda match: (match["segmentByteStart"], match["segmentByteEnd"], match["termId"], match["phrase"]))
        value = {
            "segmentId": payload.input_record.record_id, "segmentDigest": sha256_digest(payload.content),
            "resource": description.external_resources[0].to_dict(),
            "enclosingSourceEvidence": payload.evidence.to_dict(), "matches": matches,
        }
        output_bytes = len(canonical_json_bytes(value))
        if output_bytes > description.item_limits.max_output_bytes:
            raise LimitExceededError("phrase matcher output exceeds its byte bound")
        elapsed = monotonic() - started
        if elapsed > description.item_limits.max_duration_seconds:
            raise LimitExceededError("phrase matcher exceeds its duration bound")
        receipt = {
            "executionKind": "local-deterministic", "requestId": request.request_id, "reuseKey": request.reuse_key,
            "processorId": description.processor_id, "processorDescriptionDigest": identity_digest(description.to_dict()),
            "inputIds": [payload.input_record.record_id], "outputDigest": identity_digest(value),
            "outputSchemaId": description.output_schema_id, "outputMediaType": description.output_media_types[0],
            "configurationDigest": description.configuration_digest, "dataUsePolicyDigest": description.data_use_policy_digest,
            "retryPolicyDigest": description.retry_policy_digest,
        }
        record = DerivedRecord.create(
            source_item_id=request.source_item_id, processor_id=description.processor_id,
            input_ids=(payload.input_record.record_id,), schema_id=description.output_schema_id, value=value,
            provider_receipt_digest=processor_receipt_digest(receipt), disposition=ProcessorDisposition.PRODUCED,
        )
        return ProcessorResult(
            request.request_id, request.reuse_key, ProcessorDisposition.PRODUCED, description.output_media_types[0],
            description.external_resources, (record,), ProcessorResourceUse(len(payload.content), output_bytes, int(elapsed * 1000)),
            (), receipt,
        )
