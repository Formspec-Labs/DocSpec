"""Document algorithms expressed through Core requests, results, and states.

Document metadata remains application data. Every actual stage uses the shared
attempt, reuse, provenance and publication owners; there is no document cache.
"""

from contextlib import closing
from datetime import UTC, datetime
from dataclasses import dataclass, replace
from collections.abc import Callable

from docspec.application.core_execution import CoreOperations, ResolveCall
from docspec.application.document_run import run_document_operation, run_request_id
from docspec.domain import core
from docspec.domain.content import CapturedFile, Representation, SourceItem, SourceItemState
from docspec.domain.core_admission import admit_record, encode_record, record_value
from docspec.domain.identity import stable_urn, require_text, canonical_value_bytes
from docspec.domain.graphs import operation_order
from docspec.domain.references import BlobRef
from docspec.domain.source_catalog import SourceCatalogItem
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import BoundContentFetcher
from docspec.ports.record_storage import bounded_rows
from docspec.processing.artifacts import RepresentationPayload
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry


def _id(kind, value):
    return stable_urn("core-document-" + kind, value)


def _definition(implementation, configuration, *, kind="transformation"):
    return core.OperationDefinition(format_version=1, definition_id=_id("definition", [implementation, configuration, kind]),
        implementation_id=implementation, implementation_version="1", operation_kind=kind, configuration=configuration)


def _outputs(resolution):
    return {output.label: output.entity_id for output in resolution.result.outcome.outputs}


def _successful(result):
    return result.outcome.status == "success"


@dataclass(frozen=True)
class DocumentProcessor:
    """A named document operation; the callback uses the ordinary Core context.

    Inputs include the segments state and each prerequisite's selected output.
    Policies, resources, ordering requirements and limits belong in the pinned
    definition. Actual reads and generated outputs belong in the callback.
    """
    name: str
    definition: core.OperationDefinition
    process: Callable
    dependencies: tuple[str, ...] = ()

    def __post_init__(self):
        """Admit the definition and refuse a non-transformation or non-callable processor."""
        require_text(self.name, "processor name")
        definition = admit_record(encode_record(self.definition))
        if not isinstance(definition, core.OperationDefinition) or definition.operation_kind != "transformation":
            raise ValueError("document processor requires a transformation definition")
        if not callable(self.process):
            raise TypeError("processor implementation must be callable")
        object.__setattr__(self, "definition", definition)
        object.__setattr__(self, "dependencies", tuple(self.dependencies))


class DocumentPipeline:
    """Run document capture, extraction, segmentation and processor stages through Core resolution."""
    def __init__(self, publisher, *, fetcher, extractor=None, segmenter=None, max_file_bytes=64 * 1024**2):
        self.publisher, self.operations = publisher, CoreOperations(publisher)
        self.fetcher = BoundContentFetcher(fetcher)
        self.extractor = extractor or DefaultExtractorRegistry()
        self.segmenter = segmenter or DefaultSegmenterRegistry()
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.max_file_bytes = max_file_bytes

    def _value(self, session, entity_id):
        """Read and decode one entity value, refusing an unavailable input."""
        with owned_iterator(session.read_records([("entity", entity_id)])) as rows:
            row = next(rows)[0]
        if row is None or not row.available:
            raise IntegrityError("document input is unavailable")
        return row.value.value.value if isinstance(row.value.value, core.InlineValue) else session.read_json(row.value.value)

    def _stage(self, session, *, definition, identity, inputs, producer, target, fresh=False, dependencies=None):
        """Build the ResolveCall for one stage, deriving whole-input dependencies by default."""
        bindings = tuple(core.StateInput(label=label, state_id=value.state_id) if isinstance(value, core.State)
            else core.WholeInput(label=label, entity_id=value) for label, value in inputs.items())
        request = core.Request(format_version=1, request_id=identity + ":request", definition_id=definition.definition_id,
            inputs=bindings, dependencies=dependencies if dependencies is not None else tuple(
                core.Dependency(label=binding.label, binding_label=binding.label, selection=core.Whole()) for binding in bindings))
        return ResolveCall(definition, request, producer, selection_id=identity + ":selection", target=target,
            reuse_policy=_successful, fresh=fresh, capture_origin=target if definition.operation_kind == "capture" else None)

    def _capture_call(self, session, source_id, candidate_index, *, identity, fresh=False, observe_source_bytes=None):
        """Build the capture call for one candidate, checking any expected digest and size."""
        source = SourceItem.from_dict(self._value(session, source_id))
        candidate = source.candidates[candidate_index]
        fetch_identity = self.fetcher.verify_configuration()
        definition = _definition(fetch_identity["implementationId"], {**fetch_identity, "maxBytes": self.max_file_bytes}, kind="capture")
        # Source metadata does not affect acquisition; source identity, version,
        # candidate order and every candidate input remain material.
        selection = core.JsonFields(selectors=tuple(core.Field(label=name, pointer="/" + name)
            for name in ("itemId", "version", "state", "candidates")))
        dependencies = (core.Dependency(label="source", binding_label="source", selection=selection),)
        definition = _definition(definition.implementation_id, {**definition.configuration, "candidateIndex": candidate_index}, kind="capture")

        def acquire(context):
            context.read_value(source_id)
            with self.fetcher.fetch(candidate, max_bytes=self.max_file_bytes,
                    task_id=context.execution.request_id, attempt_id=context.execution.execution_id) as stream:
                def chunks():
                    with owned_iterator(stream.chunks) as source:
                        for chunk in source:
                            if observe_source_bytes is not None:
                                observe_source_bytes(len(chunk))
                            yield chunk
                value = session.retain_bytes(chunks(), media_type=candidate.media_type)
                if candidate.expected_digest is not None and value.digest != candidate.expected_digest:
                    raise IntegrityError("captured bytes differ from the expected digest")
                if candidate.expected_size is not None and value.byte_size != candidate.expected_size:
                    raise IntegrityError("captured bytes differ from the expected size")
                context.generate(value, label="content", role="raw")
                captured = CapturedFile.create(source_item_id=source.item_id, source_version=source.version,
                    candidate_id=candidate.candidate_id, blob=BlobRef(value.locator, value.digest, value.byte_size, value.media_type),
                    media_type=candidate.media_type, acquired_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    downloader_id=stream.metadata.downloader_id, transport_version=stream.metadata.transport_version,
                    acquisition_started_at=stream.metadata.acquisition_started_at,
                    downloader_configuration_digest=stream.metadata.downloader_configuration_digest,
                    task_id=stream.metadata.task_id, attempt_id=stream.metadata.attempt_id)
                context.generate(core.InlineValue(value=captured.to_dict()), label="capture", role="raw")
        return self._stage(session, definition=definition, identity=identity, inputs={"source": source_id},
            producer=acquire, target=core.Origin(parent_entity_id=source_id), fresh=fresh, dependencies=dependencies)

    def _extract_call(self, session, capture, *, identity, fresh=False):
        """Build the extraction call selected by the captured file's extractor identity."""
        inputs = _outputs(capture)
        captured = CapturedFile.from_dict(self._value(session, inputs["capture"]))
        extractor_id, configuration = self.extractor.selected_identity(captured)
        definition = _definition(extractor_id, {"configurationDigest": configuration})
        def extract(context):
            current = CapturedFile.from_dict(context.read_value(inputs["capture"]))
            with closing(context.read_chunks(inputs["content"], max_bytes=self.max_file_bytes)) as chunks:
                extracted = self.extractor.extract(current, b"".join(chunks))
            payload = extracted.payload
            content = context.generate(session.retain_bytes([payload.content], media_type=payload.representation.blob.media_type), label="content")
            metadata = context.generate(core.InlineValue(value={"representation": payload.representation.to_dict(),
                "receipt": extracted.receipt.to_dict()}), label="representation")
            for output in (content, metadata):
                context.derive(output.entity_id, inputs["content"])
        return self._stage(session, definition=definition, identity=identity, inputs=inputs, producer=extract,
            target=core.Origin(parent_entity_id=inputs["content"]), fresh=fresh)

    def _segment_call(self, session, extraction, *, identity, fresh=False):
        """Build the segmentation call that produces the keyed segments state."""
        inputs = _outputs(extraction)
        representation = Representation.from_dict(self._value(session, inputs["representation"])["representation"])
        segmenter_id, policy = self.segmenter.selected_identity(representation)
        definition = _definition(segmenter_id, {"policyDigest": policy})
        def segment(context):
            record = Representation.from_dict(context.read_value(inputs["representation"])["representation"])
            with closing(context.read_chunks(inputs["content"], max_bytes=self.max_file_bytes)) as chunks:
                payload = RepresentationPayload(record, b"".join(chunks))
            segments = self.segmenter.segment(payload)
            state_id = context.execution.execution_id + ":segments"
            def rows():
                for index, item in enumerate(segments):
                    prefix = f"{index:012d}"
                    content_id = state_id + ":content:" + prefix
                    yield prefix + ":content", core.Entity(format_version=1, entity_id=content_id,
                        entity_type="occurrence", value=session.retain_bytes([item.content], media_type=item.segment.content.media_type))
                    yield prefix + ":metadata", core.Entity(format_version=1, entity_id=state_id + ":metadata:" + prefix,
                        entity_type="occurrence", value=core.InlineValue(value={"segment": item.segment.to_dict(), "contentEntityId": content_id}))
            state = self.publisher.states.create_keyed(session, state_id=state_id, representation_id=state_id + ":physical",
                unit_id=state_id + ":import", rows=rows())
            context.generate_record(state, label="segments")
            context.derive(state.state_id, inputs["content"])
        return self._stage(session, definition=definition, identity=identity, inputs=inputs, producer=segment,
            target=core.Origin(parent_entity_id=inputs["representation"]), fresh=fresh)

    def _processor_call(self, session, segmentation, processor, parents, *, identity, fresh=False):
        """Build one processor call over the segments state and its declared parent outputs."""
        segments_id = _outputs(segmentation)["segments"]
        inputs = {"segments": core.State(format_version=1, state_id=segments_id)}
        for parent in processor.dependencies:
            for label, entity_id in _outputs(parents[parent]).items():
                with owned_iterator(session.read_records([("state", entity_id)])) as rows:
                    state = next(rows)[0]
                inputs[parent + ":" + label] = state.value if state is not None else entity_id
        def produce(context):
            return processor.process(context, inputs)
        return self._stage(session, definition=processor.definition, identity=identity + ":" + processor.name,
            inputs=inputs, producer=produce, target=core.Origin(parent_entity_id=segments_id), fresh=fresh)

    def rows(self, state_id):
        """Yield decoded rows of a retained state under a publication session."""
        with self.publisher.session() as session:
            with closing(self.publisher.states.rows(session, state_id)) as rows:
                for key, entity in rows:
                    yield key, entity.entity_id, entity.value.value if isinstance(entity.value, core.InlineValue) else session.read_json(entity.value)

    def retained_roots(self, run_state_id):
        """Declare the exact records needed to retain or export a document run.

        Core states contain arbitrary values. This document-owned interpretation
        supplies source and stage roots explicitly, including inactive sources
        that have no stage selections. Consumers may deduplicate this stream.
        """
        yield "state", run_state_id
        with owned_iterator(self.publisher.ledger.executions(run_request_id(run_state_id))) as batches:
            for batch in batches:
                for execution_id in batch:
                    yield "result", execution_id + ":result"
        with closing(self.rows(run_state_id)) as rows:
            for _, _, value in rows:
                if not isinstance(value, dict) or set(value) != {"sourceEntityId", "sourceItemId", "selections"}:
                    raise IntegrityError("document run summary has an invalid shape")
                require_text(value["sourceEntityId"], "document source entity identity")
                require_text(value["sourceItemId"], "document source item identity")
                if not isinstance(value["selections"], list):
                    raise IntegrityError("document run selections must be a list")
                yield "entity", value["sourceEntityId"]
                for selection_id in value["selections"]:
                    require_text(selection_id, "document selection identity")
                    yield "selection", selection_id

    def import_sources(self, items, *, state_id):
        """Import source items as one keyed occurrence state."""
        def entities():
            with owned_iterator(items) as source_items:
                for item in source_items:
                    source = item.to_processing_item() if isinstance(item, SourceCatalogItem) else item
                    source = source if isinstance(source, SourceItem) else SourceItem.from_dict(source)
                    value = source.to_dict()
                    yield source.item_id, core.Entity(format_version=1, entity_id=_id("source", value),
                        entity_type="occurrence", value=core.InlineValue(value=value))
        with self.publisher.session() as session:
            return self.publisher.states.create_keyed(session, state_id=state_id, representation_id=state_id + ":physical",
                unit_id=state_id + ":import", rows=entities())

    def run(self, source_state_id, *, run_id, dataset=None, extract=True, segment=True, fresh=False, processors=(),
            max_source_bytes=None, max_generated_rows=None):
        """Process one pinned run with cumulative actual-work limits across resume.

        New source chunks and producer bulk rows consume the budget; reuse,
        source import and final summary assembly do not. A new run ID starts a
        new budget. Uncheckpointed work after a hard kill refuses continuation.
        """
        for name, limit in (("max_source_bytes", max_source_bytes), ("max_generated_rows", max_generated_rows)):
            if limit is not None and (type(limit) is not int or limit < 0):
                raise ValueError(name + " must be a non-negative integer or None")
        require_text(run_id, "document run identity")
        processors = tuple(processors)
        if len({processor.name for processor in processors}) != len(processors):
            raise ValueError("processor names must be distinct")
        if processors and not segment:
            raise ValueError("document processors require segmentation")
        if segment and not extract:
            raise ValueError("segmentation requires extraction")
        by_name = {processor.name: processor for processor in processors}
        order = operation_order({name: processor.dependencies for name, processor in by_name.items()}, label="document processors")
        configuration = {
            "dataset": dataset, "extract": extract, "segment": segment, "fresh": fresh,
            "max_source_bytes": max_source_bytes, "max_generated_rows": max_generated_rows,
            "max_file_bytes": self.max_file_bytes, "fetcher": self.fetcher.verify_configuration(),
            "extractor": {"id": self.extractor.extractor_id, "configuration": self.extractor.configuration_digest},
            "segmenter": {"id": self.segmenter.segmenter_id, "configuration": self.segmenter.policy_digest},
            "processors": [{"name": item.name, "definition": record_value(item.definition),
                            "dependencies": list(item.dependencies)} for item in processors],
        }
        return run_document_operation(self.operations, run_id=run_id, source_state_id=source_state_id,
            configuration=configuration, produce=lambda context, counts: self._assemble(
                source_state_id, run_id=run_id, extract=extract, segment=segment, fresh=fresh,
                by_name=by_name, order=order, counts=counts,
                max_source_bytes=max_source_bytes, max_generated_rows=max_generated_rows))

    def _assemble(self, source_state_id, *, run_id, extract, segment, fresh, by_name, order,
                  counts, max_source_bytes, max_generated_rows):
        """Assemble the run's summary state within the cumulative actual-work budgets."""
        def check():
            if max_source_bytes is not None and counts["source_bytes"] > max_source_bytes:
                raise LimitExceededError("document run exceeds its new source byte limit")
            if max_generated_rows is not None and counts["generated_rows"] > max_generated_rows:
                raise LimitExceededError("document run exceeds its new generated row limit")
        check()
        def source_bytes(count):
            counts["source_bytes"] += count
            check()
        def generated_row():
            counts["generated_rows"] += 1
            check()
        def budgeted(calls):
            with owned_iterator(calls) as source:
                for call in source:
                    def produce(context, producer=call.producer):
                        with context.session.observe_generated_rows(generated_row):
                            return producer(context)
                    yield replace(call, producer=produce)
        def results():
            with closing(bounded_rows(self.rows(source_state_id), size=lambda row: len(canonical_value_bytes(row[2])), max_rows=32)) as groups:
                for group in groups:
                    history = {key: {} for key, _, _ in group}
                    work_items = ((key, source_id, index, _id("attempt", [run_id, key, index]))
                        for key, source_id, source_value in group
                        if (source := SourceItem.from_dict(source_value)).state == SourceItemState.ACTIVE
                        for index in range(len(source.candidates)))
                    with closing(bounded_rows(work_items, size=lambda row: len(canonical_value_bytes(row)), max_rows=32)) as work_groups:
                        for work in work_groups:
                            with self.publisher.session() as active:
                                calls = (self._capture_call(active, source_id, index, identity=prefix + ":capture", fresh=fresh,
                                    observe_source_bytes=source_bytes)
                                    for key, source_id, index, prefix in work)
                                with closing(self.operations.resolve_many(budgeted(calls), session=active)) as resolved:
                                    current = list(resolved)
                                for (key, _, index, _), resolution in zip(work, current, strict=True):
                                    history[key].setdefault(index, []).append(resolution.selection.selection_id)
                                for enabled, name, prepare in ((extract, "extract", self._extract_call), (segment, "segment", self._segment_call)):
                                    if not enabled:
                                        continue
                                    calls = (prepare(active, previous, identity=entry[3] + ":" + name, fresh=fresh)
                                        for entry, previous in zip(work, current, strict=True))
                                    with closing(self.operations.resolve_many(budgeted(calls), session=active)) as resolved:
                                        current = list(resolved)
                                    for (key, _, index, _), resolution in zip(work, current, strict=True):
                                        history[key][index].append(resolution.selection.selection_id)
                                processor_results = [{} for _ in work]
                                for name in order:
                                    calls = (self._processor_call(active, segmentation, by_name[name], parents,
                                        identity=entry[3] + ":processors", fresh=fresh)
                                        for entry, segmentation, parents in zip(work, current, processor_results, strict=True))
                                    with closing(self.operations.resolve_many(budgeted(calls), session=active)) as resolved:
                                        for entry, parents, result in zip(work, processor_results, resolved, strict=True):
                                            parents[name] = result
                                            history[entry[0]][entry[2]].append(result.selection.selection_id)
                    for key, source_id, source_value in group:
                        value = {"sourceEntityId": source_id, "sourceItemId": source_value["itemId"], "selections": [selected for index in sorted(history[key]) for selected in history[key][index]]}
                        yield key, core.Entity(format_version=1, entity_id=_id("summary", [run_id, key]),
                            entity_type="occurrence", value=core.InlineValue(value=value))
        with self.publisher.session() as session:
            state = self.publisher.states.create_keyed(session, state_id=run_id, representation_id=run_id + ":physical",
                unit_id=run_id + ":publication", rows=results())
            return state
