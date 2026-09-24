"""A field-limited provider callback adapted to one Core document operation."""

from contextlib import closing
from msgspec.structs import replace
from fnmatch import fnmatchcase
from time import monotonic

from docspec.adapters.schema_validation import compile_payload_schema, validate_payload
from docspec.application.document_processors import SEGMENT_ORDER, segment_rows
from docspec.application.documents import DocumentProcessor
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record, record_value
from docspec.domain.identity import canonical_value_bytes, snapshot_json_value, stable_urn
from docspec.domain.processor_policy import DataUsePolicy, ProcessorLimits, ProcessorResponse
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError, LimitExceededError


def provider_processor(name, *, definition, process, data_use_policy: DataUsePolicy, output_schema,
                       input_media_types=("*/*",), output_media_type="application/json", limits=ProcessorLimits()):
    """Give the provider only permitted fields; retain validated outputs/evidence.

    Calls are bounded per segment. The operation definition pins policy, schema,
    limits and resource descriptions. The ledger owns attempts and reuse.
    """
    definition = admit_record(encode_record(definition))
    policy = DataUsePolicy.from_dict(data_use_policy.to_dict())
    schema = snapshot_json_value(output_schema)
    validator = compile_payload_schema(schema)
    media = tuple(input_media_types)
    external = policy.allows_external_processing
    settings = {"dataUsePolicy": policy.to_dict(), "outputSchema": schema, "inputMediaTypes": list(media),
                "outputMediaType": output_media_type, "limits": limits.to_dict(), "segmentOrder": SEGMENT_ORDER}
    configuration = {**definition.configuration, "documentProcessor": settings}
    definition = replace(definition, definition_id=stable_urn("core-document-processor", [definition.definition_id, configuration]),
                         configuration=configuration)
    expected_resources = tuple(record_value(resource, core.Resource) for resource in definition.resources)
    def produce(context, inputs):
        session = context.session
        state_id = inputs["segments"].state_id
        context.use(state_id)
        output_id = context.execution.execution_id + ":records"
        def rows():
            with closing(segment_rows(session, state_id)) as segments:
                for key, segment, reference in segments:
                    if not any(fnmatchcase(segment.content.media_type, pattern) for pattern in media):
                        raise IntegrityError("processor does not accept segment media type")
                    projected = {
                        "contentMediaType": segment.content.media_type, "evidence": segment.evidence.to_dict(),
                        "representationCoordinates": [segment.representation_start, segment.representation_end],
                        "segmentKind": segment.kind, "segmentOrdinal": segment.ordinal,
                    }
                    projected = {field: value for field, value in projected.items() if field in policy.allowed_fields}
                    byte_size = len(canonical_value_bytes(projected))
                    if "content" in policy.allowed_fields:
                        if byte_size + reference.byte_size > limits.max_input_bytes:
                            raise LimitExceededError("processor input exceeds its byte limit")
                        with owned_iterator(session.blobs.read(reference, max_bytes=limits.max_input_bytes)) as chunks:
                            projected["content"] = b"".join(chunks)
                        byte_size += reference.byte_size
                    if byte_size > limits.max_input_bytes:
                        raise LimitExceededError("processor input exceeds its byte limit")
                    started = monotonic()
                    response = process(projected)
                    duration = monotonic() - started
                    if not isinstance(response, ProcessorResponse):
                        raise IntegrityError("processor must return ProcessorResponse")
                    policy.require_provider_evidence(response.provider_evidence, external=external)
                    if external != (response.external_request_count > 0):
                        raise IntegrityError("processor external request count differs from its execution scope")
                    if response.media_type != output_media_type or response.resources != expected_resources:
                        raise IntegrityError("processor output media or resources differ from its definition")
                    if duration > limits.max_duration_seconds:
                        raise LimitExceededError("processor exceeds its duration limit")
                    values = bounded_items(response.values, limit=limits.max_output_records)
                    total = 0
                    admitted = []
                    for value in values:
                        value = snapshot_json_value(value)
                        total += len(canonical_value_bytes(value))
                        if total > limits.max_output_bytes:
                            raise LimitExceededError("processor output exceeds its byte limit")
                        validate_payload(validator, value, "processor output")
                        admitted.append(value)
                    # The evidence record remains separate even for zero values.
                    evidence = {"segmentId": segment.segment_id, "inputBytes": byte_size, "outputBytes": total,
                        "outputCount": len(admitted), "durationMilliseconds": int(duration * 1000),
                        "externalRequestCount": response.external_request_count,
                        "providerEvidence": None if response.provider_evidence is None else response.provider_evidence.to_dict()}
                    for suffix, value in [("receipt", evidence), *((f"output:{i}", value) for i, value in enumerate(admitted))]:
                        yield key + ":" + suffix, core.Entity(format_version=1, entity_id=output_id + ":" + key + ":" + suffix,
                            entity_type="occurrence", value=core.InlineValue(value=value))
        session.mint(output_id)
        state = session.states.create_keyed(session, state_id=output_id, representation_id=output_id + ":physical",
            unit_id=output_id + ":import", rows=rows())
        context.generate_record(state, label="records")
        context.derive(state.state_id, state_id)
    return DocumentProcessor(name, definition, produce)
