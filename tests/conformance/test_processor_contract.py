from __future__ import annotations

import importlib
import sys
from pathlib import Path

from docspec.domain.content import AcquisitionDisposition, DerivedRecord, ProcessorDisposition, SourceItem
from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetryPolicy
from docspec.domain.processors import ProcessorRecordRef, ProcessorResourceUse, ProcessorResult
from docspec.domain.receipts import RunReceipt
from docspec.processing.processors import ContentStatisticsProcessor

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_helpers = importlib.import_module("tests.helpers")
SharedFixtureContentFetcher = _helpers.SharedFixtureContentFetcher
_equivalence = importlib.import_module("tests.conformance.test_incremental_equivalence")
_document_store = importlib.import_module("tests.conformance.test_document_store")
_pipeline_helpers = importlib.import_module("tests.test_application_pipeline")
_processor_helpers = importlib.import_module("tests.test_processor_reprocessing")
_platform = _equivalence._platform
_reconciled_counts = _document_store._reconciled_counts
_run = _pipeline_helpers._run
_write_source = _pipeline_helpers._write_source
_CountingProcessor = _processor_helpers._CountingProcessor
_description = _processor_helpers._description
_plan = _processor_helpers._plan


class _RequestRecordingProcessor(_CountingProcessor):
    """The shared counting processor, retaining every request it receives."""

    def __init__(self, description) -> None:
        super().__init__(description)
        self.requests = []

    def process(self, request, payload, prerequisite_results):
        self.requests.append(request)
        return super().process(request, payload, prerequisite_results)


class _AbstainingProcessor(_CountingProcessor):
    """The shared counting processor, abstaining for one declared item."""

    def __init__(self, description, *, abstain_item_id: str) -> None:
        super().__init__(description)
        self._abstain_item_id = abstain_item_id

    def process(self, request, payload, prerequisite_results):
        produced = super().process(request, payload, prerequisite_results)
        if request.source_item_id != self._abstain_item_id:
            return produced
        value = {"reason": "the fixture declares nothing to produce"}
        receipt = dict(produced.provider_receipt)
        receipt["outputDigest"] = identity_digest(value)
        template = produced.derived_records[0]
        abstained = DerivedRecord.create(
            source_item_id=template.source_item_id,
            processor_id=template.processor_id,
            input_ids=template.input_ids,
            schema_id=template.schema_id,
            value=value,
            provider_receipt_digest=identity_digest(receipt),
            disposition=ProcessorDisposition.ABSTAINED,
        )
        return ProcessorResult(
            produced.request_id,
            produced.reuse_key,
            ProcessorDisposition.ABSTAINED,
            produced.output_media_type,
            produced.resource_identities,
            (abstained,),
            ProcessorResourceUse(
                produced.resource_use.input_bytes,
                len(canonical_json_bytes(value)),
                0,
            ),
            (),
            receipt,
        )


def _seeded_items(platform, texts: dict[str, str]) -> tuple[SourceItem, ...]:
    items = []
    for item_id in sorted(texts):
        candidate = _write_source(platform.sources / f"{item_id}.txt", texts[item_id])
        items.append(SourceItem(item_id, "v1", (candidate,), metadata={"expectedSegments": 1}))
    return tuple(items)


def _sealed_entries(platform, sealed) -> dict[str, object]:
    entries = {}
    for reference in sealed:
        for entry in platform.stores.load(reference).entries:
            entries[entry.source_item.item_id] = entry
    return entries


def _run_processors(platform, processors, plan):
    return _run(
        plan=plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=SharedFixtureContentFetcher(platform.sources),
        processors=processors,
        partition_policy=platform.partition_policy,
    )


def test_fake_and_real_processors_pass_one_reference_based_contract(tmp_path: Path) -> None:
    """One pipeline drives the fake counting adapter and the real statistics
    adapter through the same reference-based request contract, and each seals
    provenance-complete derived records into its own separate derived layer."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    adapters = (
        ("fake", _RequestRecordingProcessor(_description("contract-fake", "1", retry))),
        ("real", ContentStatisticsProcessor(retry_policy=retry)),
    )
    for label, processor in adapters:
        platform = _platform(tmp_path / label, member_bytes=1024 * 1024)
        items = _seeded_items(platform, {"document-contract": "One paragraph carries the whole fixture."})
        source = platform.publish_source(items)
        plan = _plan(source, None, (processor,), retry, accepted)
        _, _, sealed, _, release_ref = _run_processors(platform, (processor,), plan)

        (entry,) = _sealed_entries(platform, sealed).values()
        assert entry.disposition is AcquisitionDisposition.CAPTURED
        (segment,) = entry.segments
        (record,) = entry.derived_records
        description = processor.description
        assert record.processor_id == description.processor_id, "the record pins the processor description"
        assert record.schema_id == description.output_schema_id
        assert record.input_ids == (segment.segment_id,), "the record pins its exact input records"
        assert record.disposition is ProcessorDisposition.PRODUCED
        assert record.output_digest == identity_digest(record.value)
        assert record.provider_receipt_digest, "the record pins the provider receipt"

        rows = list(platform.catalog.scan(release_ref, layer_kind=f"derived:{description.processor_id}"))
        assert [row["payload"]["derivedId"] for row in rows] == [record.derived_id], (
            f"the {label} adapter's records live in their own derived layer"
        )

        if label != "fake":
            continue
        (request,) = processor.requests
        assert request.plan.artifact_id == plan.plan_id
        assert request.processor_id == description.processor_id
        assert request.processor_description_digest == identity_digest(description.to_dict())
        assert request.input_records == (ProcessorRecordRef.for_segment(segment),), (
            "the request names exact input identities and digests, not bulk content"
        )
        assert request.prerequisite_results == ()
        assert request.allowed_fields == DataUsePolicy.local_content().allowed_fields
        assert request.item_limits == description.item_limits
        assert request.invocation_id, "every invocation carries its own identity"


def test_dependency_outputs_flow_only_through_declared_edges(tmp_path: Path) -> None:
    """A downstream processor receives exactly its declared prerequisite's
    sealed results -- never an undeclared sibling's -- and every processor's
    records land in a separate derived layer."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    upstream = _RequestRecordingProcessor(_description("contract-upstream", "1", retry))
    sibling = _RequestRecordingProcessor(_description("contract-sibling", "1", retry))
    downstream = _RequestRecordingProcessor(
        _description(
            "contract-downstream",
            "1",
            retry,
            dependencies=(upstream.description.processor_id,),
        )
    )
    processors = (upstream, sibling, downstream)
    platform = _platform(tmp_path, member_bytes=1024 * 1024)
    items = _seeded_items(platform, {"document-graph": "One paragraph feeds the processor graph."})
    source = platform.publish_source(items)
    plan = _plan(source, None, processors, retry, accepted)
    assert plan.processors.execution_order[-1].processor_id == downstream.description.processor_id, (
        "the plan pins the acyclic graph before execution"
    )
    _, _, sealed, _, release_ref = _run_processors(platform, processors, plan)

    (entry,) = _sealed_entries(platform, sealed).values()
    records = {record.processor_id: record for record in entry.derived_records}
    assert set(records) == {item.description.processor_id for item in processors}
    (segment,) = entry.segments

    (upstream_request,) = upstream.requests
    (sibling_request,) = sibling.requests
    (downstream_request,) = downstream.requests
    assert upstream_request.prerequisite_results == ()
    assert sibling_request.prerequisite_results == ()
    assert len(downstream_request.prerequisite_results) == 1, (
        "the downstream request carries exactly its one declared prerequisite"
    )

    upstream_record = records[upstream.description.processor_id]
    sibling_record = records[sibling.description.processor_id]
    downstream_record = records[downstream.description.processor_id]
    assert downstream_record.input_ids == (segment.segment_id, upstream_record.derived_id), (
        "the downstream record pins its segment and declared dependency outputs"
    )
    assert sibling_record.derived_id not in downstream_record.input_ids, (
        "an undeclared sibling's output never reaches the downstream processor"
    )
    assert upstream_record.input_ids == (segment.segment_id,)

    for processor in processors:
        layer_kind = f"derived:{processor.description.processor_id}"
        rows = list(platform.catalog.scan(release_ref, layer_kind=layer_kind))
        assert [row["payload"]["derivedId"] for row in rows] == [
            records[processor.description.processor_id].derived_id
        ]


def test_every_scheduled_item_ends_in_one_registered_processor_disposition(tmp_path: Path) -> None:
    """A processor that abstains for one item and produces for another leaves
    each scheduled item with exactly one registered terminal disposition, and
    the run receipt reconciles both."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processor = _AbstainingProcessor(
        _description("contract-dispositions", "1", retry),
        abstain_item_id="document-abstain",
    )
    platform = _platform(tmp_path, member_bytes=1024 * 1024)
    items = _seeded_items(
        platform,
        {
            "document-abstain": "This paragraph earns an abstention.",
            "document-produce": "This paragraph earns a produced record.",
        },
    )
    source = platform.publish_source(items)
    plan = _plan(source, None, (processor,), retry, accepted)
    _, _, sealed, run_ref, release_ref = _run_processors(platform, (processor,), plan)

    entries = _sealed_entries(platform, sealed)
    dispositions = {
        item_id: tuple(record.disposition for record in entry.derived_records)
        for item_id, entry in entries.items()
    }
    assert dispositions == {
        "document-abstain": (ProcessorDisposition.ABSTAINED,),
        "document-produce": (ProcessorDisposition.PRODUCED,),
    }
    assert all(entry.disposition is AcquisitionDisposition.CAPTURED for entry in entries.values())

    layer_kind = f"derived:{processor.description.processor_id}"
    layer_dispositions = {
        row["payload"]["sourceItemId"]: row["payload"]["disposition"]
        for row in platform.catalog.scan(release_ref, layer_kind=layer_kind)
    }
    assert layer_dispositions == {
        "document-abstain": "abstained",
        "document-produce": "produced",
    }

    run = RunReceipt.from_dict(platform.controls.load(run_ref))
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["derivedRecords"] == 2
    assert run.counts["acceptedFailureStores"] == 0
