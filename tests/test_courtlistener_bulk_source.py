"""Contracts for the CourtListener bulk-data population and its pinned capture."""

from __future__ import annotations

from pathlib import Path

import pytest

from docspec.domain.content import SourceItemState
from docspec.errors import IntegrityError
from tests.helpers import source_catalog_reader, write_shared_source_catalog
from tools.courtlistener_bulk_source import (
    CONTENT_BASE,
    BulkCapture,
    BulkObject,
    build_source_items,
    coverage_for,
    load_capture,
    parse_capture,
    parse_listing_page,
    write_capture_pins,
)

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_DIR = ROOT / "fixtures" / "courtlistener-bulk-v1"
PINS_PATH = CAPTURE_DIR / "pins.json"

# The real 2026-08-22 capture of the publisher's listing. Restated here so a
# capture that silently changes shape fails a test rather than a coverage claim.
EXPECTED_OBJECT_COUNT = 1076
EXPECTED_OPINIONS_DUMPS = 36


def _page(entries: str, *, prefix: str = "bulk-data/", truncated: bool = False, token: str | None = None) -> bytes:
    marker = "<IsTruncated>true</IsTruncated>" if truncated else "<IsTruncated>false</IsTruncated>"
    continuation = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>com-courtlistener-storage</Name><Prefix>{prefix}</Prefix>"
        f"{marker}{continuation}{entries}</ListBucketResult>"
    ).encode("utf-8")


def _entry(key: str, size: int = 10, etag: str = "abc", modified: str = "2026-06-30T04:11:47.000Z") -> str:
    return (
        f"<Contents><Key>{key}</Key><LastModified>{modified}</LastModified>"
        f'<ETag>&quot;{etag}&quot;</ETag><Size>{size}</Size></Contents>'
    )


# -- parsing the publisher's enumeration -------------------------------------


def test_listing_entry_splits_dataset_from_dump_date_and_pins_its_revision():
    obj = BulkObject("bulk-data/opinions-2026-06-30.csv.bz2", 54_561_543_156, "d41d8c-25", "2026-06-30T04:11:47.000Z")
    assert obj.dataset == "opinions"
    assert obj.dump_date is not None and obj.dump_date.isoformat() == "2026-06-30"
    assert obj.media_type == "application/x-bzip2"
    assert obj.locator == f"{CONTENT_BASE}/bulk-data/opinions-2026-06-30.csv.bz2"
    # ETag alone is not a whole-object digest for a multipart upload, so the
    # version has to combine it with size and last-modified to move when the
    # object moves.
    assert obj.transport_version == "s3-listing:d41d8c-25:54561543156:2026-06-30T04:11:47.000Z"

    undated = BulkObject("bulk-data/scotus_network.csv", 7_000, "e", "2024-04-04T00:00:00.000Z")
    assert undated.dump_date is None
    assert undated.dataset == "scotus_network"
    assert undated.media_type == "text/csv"


def test_listing_page_parses_objects_and_reports_its_continuation():
    objects, token = parse_listing_page(
        _page(_entry("bulk-data/courts-2026-06-30.csv.bz2", 81_180), truncated=True, token="NEXT")
    )
    assert [o.key for o in objects] == ["bulk-data/courts-2026-06-30.csv.bz2"]
    assert objects[0].size == 81_180
    assert token == "NEXT"

    _, done = parse_listing_page(_page(_entry("bulk-data/courts-2026-06-30.csv.bz2")))
    assert done is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_page(_entry("bulk-data/x.csv"), prefix="other/"), "covers prefix"),
        (_page("<Contents><Key>bulk-data/x.csv</Key></Contents>"), "is missing"),
        (_page(_entry("bulk-data/x.csv"), truncated=True), "names no continuation token"),
        (b"<not-xml", "not well-formed XML"),
    ],
)
def test_listing_page_refuses_a_population_it_cannot_fully_believe(payload: bytes, message: str):
    """A partly-read listing must fail loudly; a short denominator is the whole risk."""
    with pytest.raises(IntegrityError, match=message):
        parse_listing_page(payload)


def test_capture_refuses_a_truncated_tail_or_a_repeated_key():
    complete = parse_capture(
        [
            _page(_entry("bulk-data/a-2026-06-30.csv.bz2"), truncated=True, token="T"),
            _page(_entry("bulk-data/b-2026-06-30.csv.bz2")),
        ]
    )
    assert [o.key for o in complete] == ["bulk-data/a-2026-06-30.csv.bz2", "bulk-data/b-2026-06-30.csv.bz2"]

    # The last page still says "there is more" — that population was only partly seen.
    with pytest.raises(IntegrityError, match="ends on a truncated listing page"):
        parse_capture([_page(_entry("bulk-data/a-2026-06-30.csv.bz2"), truncated=True, token="T")])

    with pytest.raises(IntegrityError, match="more than once"):
        parse_capture(
            [
                _page(_entry("bulk-data/a-2026-06-30.csv.bz2"), truncated=True, token="T"),
                _page(_entry("bulk-data/a-2026-06-30.csv.bz2")),
            ]
        )

    with pytest.raises(IntegrityError, match="no listing pages"):
        parse_capture([])


# -- the pinned capture ------------------------------------------------------


def test_pinned_capture_admits_the_real_publisher_enumeration():
    capture = load_capture(PINS_PATH)
    assert capture.capture_id.startswith("urn:docspec:courtlistener-bulk-capture:v1:")
    assert len(capture.objects) == EXPECTED_OBJECT_COUNT
    datasets = capture.datasets()
    assert datasets["opinions"] == EXPECTED_OPINIONS_DUMPS
    # The dump that carries opinion text is the largest thing the publisher offers,
    # and its size is the fact every ingest bound is argued from.
    biggest = max((o for o in capture.objects if o.dataset == "opinions"), key=lambda o: o.size)
    assert biggest.size > 50 * 2**30


def test_pinned_capture_refuses_bytes_that_changed_after_pinning(tmp_path: Path):
    import shutil

    work = tmp_path / "capture"
    shutil.copytree(CAPTURE_DIR, work, symlinks=False)
    page = next((work / "capture").glob("listing-page-*.xml"))
    page.write_bytes(page.read_bytes().replace(b"<Size>", b"<Size>1", 1))

    with pytest.raises(IntegrityError, match="differs from its pinned digest|differs in size"):
        load_capture(work / "pins.json")


def test_capture_identity_is_derived_from_its_content(tmp_path: Path):
    """A capture that is edited becomes a different capture, not a changed one."""
    directory = tmp_path / "cap"
    (directory / "capture").mkdir(parents=True)
    first = directory / "capture" / "listing-page-001.xml"
    first.write_bytes(_page(_entry("bulk-data/courts-2026-06-30.csv.bz2")))
    origin = {"listingHost": "h", "capturedAt": "2026-08-22T00:00:00Z"}

    pins = write_capture_pins(directory, page_paths=[first], origin=origin)
    one = load_capture(pins)

    first.write_bytes(_page(_entry("bulk-data/courts-2026-06-30.csv.bz2", size=99)))
    two = load_capture(write_capture_pins(directory, page_paths=[first], origin=origin))

    assert one.capture_id != two.capture_id


# -- the population: missing vs refused --------------------------------------


def _capture(*keys: str, capture_id: str = "urn:test:capture") -> BulkCapture:
    return BulkCapture(
        capture_id=capture_id,
        objects=tuple(BulkObject(k, 10, "e", "2026-06-30T00:00:00.000Z") for k in keys),
        origin={},
    )


def test_population_separates_what_we_refused_from_what_the_publisher_withdrew():
    """The two are recorded differently because they have opposite remedies."""
    previous = _capture(
        "bulk-data/opinions-2026-03-31.csv.bz2",
        "bulk-data/opinions-2026-06-30.csv.bz2",
    )
    current = _capture(
        "bulk-data/opinions-2026-06-30.csv.bz2",
        "bulk-data/dockets-2026-06-30.csv.bz2",
        "bulk-data/scotus_network.csv",
    )

    items = build_source_items(current, datasets={"opinions"}, previous=previous)
    by_id = {item.item_id: item for item in items}

    # In scope and enumerated: a promise acquisition must keep.
    live = by_id["bulk-data/opinions-2026-06-30.csv.bz2"]
    assert live.state is SourceItemState.ACTIVE
    assert len(live.candidates) == 1
    assert live.candidates[0].expected_size == 10
    assert live.candidates[0].locator.startswith(CONTENT_BASE)

    # We declined it — our decision, and it says so.
    declined = by_id["bulk-data/dockets-2026-06-30.csv.bz2"]
    assert declined.state is SourceItemState.EXCLUDED
    assert declined.candidates == ()
    assert declined.metadata["exclusionReason"] == "dataset outside the requested scope"

    undated = by_id["bulk-data/scotus_network.csv"]
    assert undated.state is SourceItemState.EXCLUDED
    assert undated.metadata["exclusionReason"] == "undated one-off export, not a periodic dump"

    # The publisher withdrew it — missing, kept as a tombstone rather than dropped.
    gone = by_id["bulk-data/opinions-2026-03-31.csv.bz2"]
    assert gone.state is SourceItemState.DELETED
    assert gone.candidates == ()
    assert gone.metadata["withdrawnFromCapture"] == current.capture_id


def test_population_without_a_scope_admits_every_dated_dump():
    items = build_source_items(_capture("bulk-data/a-2026-06-30.csv.bz2", "bulk-data/b-2026-06-30.csv.bz2"))
    assert {item.state for item in items} == {SourceItemState.ACTIVE}


def test_coverage_states_the_denominator_the_publisher_supplied():
    capture = _capture("bulk-data/opinions-2026-06-30.csv.bz2", "bulk-data/dockets-2026-06-30.csv.bz2")
    items = build_source_items(capture, datasets={"opinions"})
    coverage = coverage_for(capture, items)

    assert coverage["publisherObjectCount"] == 2
    assert coverage["publisherByteTotal"] == 20
    # Only one is in scope, so the active totals must not inherit the whole listing.
    assert coverage["activeItemCount"] == 1
    assert coverage["activeByteTotal"] == 10
    assert coverage["captureId"] == capture.capture_id


# -- publishing a catalog a campaign can run over ----------------------------


def test_population_publishes_as_a_verified_shared_source_catalog(tmp_path: Path):
    capture = load_capture(PINS_PATH)
    items = build_source_items(capture, datasets={"opinions", "opinion-clusters", "courts"})
    root = tmp_path / "store"
    root.mkdir()
    catalog = source_catalog_reader(root)

    reference = write_shared_source_catalog(root, items)
    snapshot = catalog.open_snapshot(reference)
    summary = snapshot.summary

    assert summary.item_count == len(items)
    assert summary.disposition_counts["selected"] == sum(
        1 for i in items if i.state is SourceItemState.ACTIVE
    )
    assert summary.disposition_counts["excluded"] == sum(
        1 for i in items if i.state is SourceItemState.EXCLUDED
    )
    # The catalog streams back the same population it admitted.
    streamed = [item.to_processing_item() for item in snapshot.items]
    assert len(streamed) == len(items)
    assert [i.item_id for i in streamed] == [i.item_id for i in items]


# -- one real, bounded acquisition -------------------------------------------


@pytest.mark.integration
def test_a_real_enumerated_dump_acquires_through_the_https_fetcher():
    """Prove the path end to end on the smallest thing the publisher enumerates.

    The `courts` dump is ~81 KiB, so this is a real network acquisition that
    stays polite. It checks the part that a fixture cannot: that the locator and
    size this module derives from the listing are the ones the publisher actually
    serves, and that the acquisition comes back with a receipt naming the
    downloader and its sealed configuration.
    """
    from docspec.adapters.content_fetchers import (
        HttpsContentFetcher,
        HttpsContentFetcherConfig,
    )

    capture = load_capture(PINS_PATH)
    items = build_source_items(capture, datasets={"courts"})
    active = [i for i in items if i.state is SourceItemState.ACTIVE]
    newest = max(active, key=lambda i: i.metadata["dumpDate"])
    candidate = newest.candidates[0]

    config = HttpsContentFetcherConfig(
        allowed_hosts=("storage.courtlistener.com",),
        user_agent="docspec/0.2 (+https://spicy-regs.dev) courtlistener-bulk-acquisition",
    )
    fetcher = HttpsContentFetcher.from_httpx(config)
    with fetcher.fetch(candidate, max_bytes=8 * 1024**2, task_id="task-courts", attempt_id="attempt-1") as stream:
        payload = b"".join(stream.chunks)
        assert stream.metadata.downloader_id == "docspec.content-fetcher.https.v1"
        assert stream.metadata.downloader_configuration_digest == config.digest

    # bzip2 magic: the publisher served the dump the listing promised.
    assert payload[:3] == b"BZh"
    assert len(payload) == candidate.expected_size

    # The capture is only proven if it comes back receipted. A CapturedFile is
    # acquisition's receipt in this codebase, and its file_id is derived from the
    # bytes plus the pinned identity — so it can only be built if the enumeration,
    # the transport, and the payload all agree.
    from docspec.domain.content import AcquisitionDisposition, CapturedFile
    from docspec.domain.identity import sha256_digest
    from docspec.domain.references import BlobRef

    blob = BlobRef(
        locator=f"blobs/{sha256_digest(payload).removeprefix('sha256:')}",
        digest=sha256_digest(payload),
        byte_size=len(payload),
        media_type=candidate.media_type,
    )
    receipt = CapturedFile.create(
        source_item_id=newest.item_id,
        source_version=newest.version,
        candidate_id=candidate.candidate_id,
        blob=blob,
        media_type=candidate.media_type,
        acquired_at="2026-08-22T05:00:00Z",
        downloader_id=fetcher.downloader_id,
        transport_version=candidate.transport_version,
        downloader_configuration_digest=config.digest,
    )
    assert receipt.file_id.startswith("urn:docspec:captured-file:v1:")
    assert receipt.disposition is AcquisitionDisposition.CAPTURED
    assert receipt.blob.byte_size == candidate.expected_size
    assert receipt.source_item_id.startswith("bulk-data/courts-")
