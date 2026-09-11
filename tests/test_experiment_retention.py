"""Processing alternatives share captures while retaining separate verified results."""

from pathlib import Path

import pytest

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.domain.content import SourceItem
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.storage import PartitionPolicy
from docspec.errors import StaleBaseError
from tests.helpers import (
    SharedFixtureContentFetcher,
    document_release_producer,
    source_catalog_reader,
    write_shared_source_catalog,
)
from tests.support.pipeline import _run, _write_source
from tests.support.processors import (
    _CountingExtractor,
    _CountingFetcher,
    _CountingProcessor,
    _CountingSegmenter,
    _description,
    _plan,
)


def test_two_processor_alternatives_retain_the_same_base_without_refetching(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    candidate = _write_source(sources / "document.txt", "First paragraph.\n\nSecond paragraph.")
    source_root = tmp_path / "source-catalogs"
    source_root.mkdir()
    source_ref = write_shared_source_catalog(source_root, (SourceItem("document-a", "v1", (candidate,)),))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "catalog", records=records, stores=stores, controls=controls,
        producer=document_release_producer(), blobs=blobs,
    )
    fetcher = _CountingFetcher(SharedFixtureContentFetcher(sources))
    extractor, segmenter = _CountingExtractor(), _CountingSegmenter()
    retry, accepted = RetryPolicy(base_delay_milliseconds=0), AcceptedFailurePolicy()

    def process(version, base, *, select_current):
        processor = _CountingProcessor(_description("experiment", version, retry))
        plan = _plan(source_ref, base, (processor,), retry, accepted)
        *_, reference = _run(
            plan=plan, source_catalog=source_catalog_reader(source_root),
            controls=controls, stores=stores, blobs=blobs, records=records,
            catalog=catalog, fetcher=fetcher, processors=(processor,),
            extractor=extractor, segmenter=segmenter,
            partition_policy=PartitionPolicy("source-item-sha256-v1", 8),
            select_current=select_current,
        )
        assert len(processor.calls) == 2
        return reference, processor.description.processor_id

    base, _ = process("1", None, select_current=True)
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == (1, 1, 1)
    first, first_processor = process("2", base, select_current=False)
    assert catalog.current() == base
    catalog.select(first, expected_current=base)
    second, second_processor = process("3", base, select_current=False)

    assert first != second
    assert catalog.current() == first
    assert catalog.open(first).previous_release == catalog.open(second).previous_release == base
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == (1, 1, 1)
    for kind in ("files", "representations", "segments"):
        expected = [row["payload"] for row in catalog.scan(base, layer_kind=kind)]
        assert [row["payload"] for row in catalog.scan(first, layer_kind=kind)] == expected
        assert [row["payload"] for row in catalog.scan(second, layer_kind=kind)] == expected
    assert len(list(catalog.scan(first, layer_kind=f"derived:{first_processor}"))) == 2
    assert len(list(catalog.scan(second, layer_kind=f"derived:{second_processor}"))) == 2
    with pytest.raises(StaleBaseError):
        catalog.select(second, expected_current=base)
    assert catalog.current() == first
    assert catalog.select(second, expected_current=first) == second
    assert catalog.current() == second
    assert catalog.open(first).previous_release == catalog.open(second).previous_release == base
    assert len(list(catalog.scan(second, layer_kind=f"derived:{second_processor}"))) == 2
