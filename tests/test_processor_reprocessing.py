from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.execution import StoreExecutionService
from docspec.domain.content import SourceItem
from docspec.domain.identity import identity_digest
from docspec.domain.jobs import ChangeKind, EntryExecutionMode
from docspec.domain.policies import (
    AcceptedFailurePolicy,
    DataUsePolicy,
    RetryPolicy,
)
from docspec.domain.processors import (
    ProcessorResourceIdentity,
    ProcessorResourceKind,
)
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from tests.helpers import (
    SharedFixtureContentFetcher,
    document_release_producer,
    processor_payload,
    segment_processor_request,
    source_catalog_reader,
    write_shared_source_catalog,
)
from tests.support.pipeline import _run, _write_source
from tests.support.processing import _captured
from tests.support.processors import (
    _CountingExtractor,
    _CountingFetcher,
    _CountingProcessor,
    _CountingSegmenter,
    _description,
    _plan,
)


def test_processor_result_must_report_declared_media_and_resources() -> None:
    model = ProcessorResourceIdentity(
        "tests.model",
        ProcessorResourceKind.MODEL,
        "1",
        identity_digest({"model": "tests.model", "revision": "1"}),
    )
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = _CountingProcessor(
        _description(
            "resource-aware",
            "1",
            retry,
            external_resources=(model,),
            output_media_types=("application/vnd.tests.result+json",),
        )
    )
    source = b"processor declaration fixture"
    extraction = TextExtractor().extract(_captured(source, "text/plain"), source)
    segment = ParagraphSegmenter().segment(extraction.payload)[0]
    request = segment_processor_request(processor, segment)
    result = processor.process(request, processor_payload(segment), ())

    StoreExecutionService._validate_processor_result(
        result,
        request,
        processor.description,
        segment.segment,
        len(segment.content),
        (),
        data_use_policy=DataUsePolicy.local_content(),
        require_current_request=True,
    )
    with pytest.raises(IntegrityError, match="media type"):
        StoreExecutionService._validate_processor_result(
            replace(result, output_media_type="application/json"),
            request,
            processor.description,
            segment.segment,
            len(segment.content),
            (),
            data_use_policy=DataUsePolicy.local_content(),
            require_current_request=True,
        )
    with pytest.raises(IntegrityError, match="resources"):
        StoreExecutionService._validate_processor_result(
            replace(result, resource_identities=()),
            request,
            processor.description,
            segment.segment,
            len(segment.content),
            (),
            data_use_policy=DataUsePolicy.local_content(),
            require_current_request=True,
        )


def _payloads(catalog, release, layer_kind: str) -> list[dict[str, Any]]:
    return [row["payload"] for row in catalog.scan(release, layer_kind=layer_kind)]


def test_changed_processor_reuses_content_and_runs_only_it_and_dependents(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    candidate = _write_source(sources / "document.txt", "First paragraph.\n\nSecond paragraph.")
    item = SourceItem("document-a", "v1", (candidate,), metadata={"expectedSegments": 2})

    source_catalog_root = tmp_path / "source-catalogs"
    source_catalog_root.mkdir()
    source_catalog = source_catalog_reader(source_catalog_root)
    source_ref = write_shared_source_catalog(source_catalog_root, (item,))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    partition_policy = PartitionPolicy("source-item-sha256-v1", 8)
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        producer=document_release_producer(),
        blobs=blobs,
    )
    fetcher = _CountingFetcher(SharedFixtureContentFetcher(sources))
    extractor = _CountingExtractor()
    segmenter = _CountingSegmenter()
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()

    first_root = _CountingProcessor(_description("root", "1", retry))
    unaffected = _CountingProcessor(_description("unaffected", "1", retry))
    first_dependent = _CountingProcessor(
        _description("dependent", "1", retry, dependencies=(first_root.description.processor_id,))
    )
    first_processors = (first_root, unaffected, first_dependent)
    first_plan = _plan(source_ref, None, first_processors, retry, accepted)
    _, _, _, _, first_release = _run(
        plan=first_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=first_processors,
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )

    base_payloads = {
        kind: _payloads(catalog, first_release, kind)
        for kind in ("files", "representations", "segments")
    }
    initial_counts = (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls))
    assert initial_counts == (1, 1, 1, 2)

    changed_root = _CountingProcessor(_description("root", "2", retry))
    changed_dependent = _CountingProcessor(
        _description("dependent", "2", retry, dependencies=(changed_root.description.processor_id,))
    )
    second_processors = (changed_root, unaffected, changed_dependent)
    second_plan = _plan(source_ref, first_release, second_processors, retry, accepted)
    planned, _, _, _, second_release = _run(
        plan=second_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=second_processors,
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )

    entry = stores.load(planned[0]).entries[0]
    assert entry.change == ChangeKind.REPAIR
    assert entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY
    assert set(entry.requested_stages.processor_ids) == {
        changed_root.description.processor_id,
        changed_dependent.description.processor_id,
    }
    assert (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls)) == initial_counts
    assert len(changed_root.calls) == len(changed_dependent.calls) == 2
    expected_ranges = [
        (payload["representationStart"], payload["representationEnd"])
        for payload in base_payloads["segments"]
    ]
    assert changed_root.representation_ranges == expected_ranges
    assert changed_dependent.representation_ranges == expected_ranges
    assert any(start > 0 for start, _ in expected_ranges)
    assert all(_payloads(catalog, second_release, kind) == base_payloads[kind] for kind in base_payloads)

    release = catalog.open(second_release)
    layer_kinds = {layer.layer_kind for layer in release.active_layers}
    assert f"derived:{first_root.description.processor_id}" not in layer_kinds
    assert f"derived:{first_dependent.description.processor_id}" not in layer_kinds
    assert f"derived:{changed_root.description.processor_id}" in layer_kinds
    assert f"derived:{changed_dependent.description.processor_id}" in layer_kinds
    assert _payloads(catalog, second_release, f"derived:{unaffected.description.processor_id}") == _payloads(
        catalog,
        first_release,
        f"derived:{unaffected.description.processor_id}",
    )

    processor_call_counts = {
        item.description.processor_id: len(item.calls)
        for item in (*first_processors, changed_root, changed_dependent)
    }
    removal_plan = _plan(source_ref, second_release, (), retry, accepted)
    removal_planned, _, _, _, removal_release = _run(
        plan=removal_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=(),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    removal_entry = stores.load(removal_planned[0]).entries[0]
    assert removal_entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY
    assert removal_entry.requested_stages.processor_ids == ()
    assert not any(layer.layer_kind.startswith("derived:") for layer in catalog.open(removal_release).active_layers)
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == initial_counts[:3]
    assert {
        item.description.processor_id: len(item.calls)
        for item in (*first_processors, changed_root, changed_dependent)
    } == processor_call_counts

    added = _CountingProcessor(_description("added", "1", retry))
    addition_plan = _plan(source_ref, removal_release, (added,), retry, accepted)
    addition_planned, _, _, _, addition_release = _run(
        plan=addition_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=(added,),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    addition_entry = stores.load(addition_planned[0]).entries[0]
    assert addition_entry.requested_stages.processor_ids == (added.description.processor_id,)
    assert len(added.calls) == 2
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == initial_counts[:3]

    renamed = _CountingProcessor(_description("renamed", "1", retry))
    rename_plan = _plan(source_ref, addition_release, (renamed,), retry, accepted)
    rename_planned, _, _, _, rename_release = _run(
        plan=rename_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=(renamed,),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    rename_entry = stores.load(rename_planned[0]).entries[0]
    assert rename_entry.requested_stages.processor_ids == (renamed.description.processor_id,)
    rename_layers = {layer.layer_kind for layer in catalog.open(rename_release).active_layers}
    assert f"derived:{added.description.processor_id}" not in rename_layers
    assert f"derived:{renamed.description.processor_id}" in rename_layers
    assert len(added.calls) == 2
    assert len(renamed.calls) == 2
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == initial_counts[:3]
