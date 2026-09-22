"""Document processor algorithms bound to Core bulk inputs and outputs."""

from contextlib import closing, contextmanager

from docspec.application.documents import DocumentProcessor, _definition
from docspec.ports.record_storage import bounded_batches
from docspec.domain import core
from docspec.errors import IntegrityError
from docspec.domain.content import Segment
from docspec.domain.core_admission import admit_record
from docspec.domain.references import BlobRef
from docspec.domain.streams import owned_iterator
from docspec.processing.statistics import content_statistics
from docspec.processing.artifacts import SegmentPayload


# Retained configuration and execution share this one consumed-order rule.
SEGMENT_ORDER = {"key": "member_key", "direction": "ASC", "collation": "binary",
                 "missingKeys": "forbidden", "duplicateKeys": "forbidden"}


@contextmanager
def segment_relation(session, state_id):
    """Join retained document metadata to content once in the bulk engine."""
    with session.states.relation(session, state_id) as relation:
        metadata = relation.filter("ends_with(member_key, ':metadata')").project(
            "member_key AS metadata_key, occurrence_record AS metadata_record, "
            "json_extract_string(decode(occurrence_record)::JSON, '$.value.value.contentEntityId') AS content_id")
        content = relation.project("occurrence_id AS content_key, occurrence_record AS content_record")
        yield metadata.set_alias("metadata").join(content.set_alias("content"), "content_id = content_key", how="left").order("metadata_key " + SEGMENT_ORDER["direction"])


def segment_rows(session, state_id):
    """Yield ``(key, segment, reference)`` rows, refusing missing or mismatched segment content."""
    with segment_relation(session, state_id) as relation:
        with closing(relation.to_arrow_reader(batch_size=256)) as batches:
            for batch in bounded_batches(batches, byte_column="metadata_record"):
                for key, metadata_payload, content_payload in zip(*(batch.column(name).to_pylist() for name in
                        ("metadata_key", "metadata_record", "content_record")), strict=True):
                    segment = Segment.from_dict(admit_record(metadata_payload).value.value["segment"])
                    if content_payload is None:
                        raise IntegrityError("segment state lacks its declared content occurrence")
                    value = admit_record(content_payload).value
                    if not isinstance(value, core.ContentRef):
                        raise IntegrityError("segment content must identify retained bytes")
                    reference = BlobRef(value.locator, value.digest, value.byte_size, value.media_type)
                    if (reference.digest, reference.byte_size, reference.media_type) != (
                            segment.content.digest, segment.content.byte_size, segment.content.media_type):
                        raise IntegrityError("segment metadata differs from its retained content reference")
                    yield key, segment, reference


def content_statistics_processor(*, max_segment_bytes=64 * 1024**2):
    """Compute the existing source-grounded statistics through one Core stage.

    The input is the complete segments state; output rows preserve its explicit
    segment positions and evidence coordinates. Bytes stay in the content store.
    """
    if type(max_segment_bytes) is not int or max_segment_bytes <= 0:
        raise ValueError("max_segment_bytes must be positive")
    definition = _definition("docspec.content-statistics", {
        "wordRule": "unicode-whitespace-separated", "maxSegmentBytes": max_segment_bytes,
        "allowedFields": ["content", "evidence"], "executionScope": "local-only",
        "outputSchema": "docspec-content-statistics/1", "segmentOrder": SEGMENT_ORDER,
    })
    def process(context, inputs):
        session = context.session
        state_id = inputs["segments"].state_id
        context.use(state_id)
        output_id = context.execution.execution_id + ":statistics"
        def rows():
            with closing(segment_rows(session, state_id)) as segments:
                for key, segment, reference in segments:
                    with owned_iterator(session.blobs.read(reference, max_bytes=max_segment_bytes)) as chunks:
                        payload = SegmentPayload(segment, b"".join(chunks))
                        stats = content_statistics(payload.content, segment.segment_id, segment.evidence)
                    yield key, core.Entity(format_version=1, entity_id=output_id + ":" + key,
                        entity_type="occurrence", value=core.InlineValue(value=stats))
        result = session.states.create_keyed(session, state_id=output_id, representation_id=output_id + ":physical",
            unit_id=output_id + ":import", rows=rows())
        context.generate_record(result, label="statistics")
        context.derive(result.state_id, state_id)
    return DocumentProcessor("content-statistics", definition, process)
