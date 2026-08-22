"""Enumerate the CourtListener bulk-data population from a pinned publisher capture.

CourtListener publishes its whole corpus as periodic CSV dumps in a public
bucket, and that bucket answers the S3 list API. The listing is therefore not a
convenience index we assembled — it is *the publisher's own enumeration of what
exists*, which is the only thing a coverage claim can honestly be measured
against. This module treats it that way: the raw listing XML is captured
verbatim, pinned by digest, and admitted only if every pinned byte still
matches, so a rewritten capture changes an identity rather than quietly changing
a population.

**Missing and refused are different, and stay different.** Three distinct
states, decided here rather than discovered at fetch time:

* ``ACTIVE`` — the publisher enumerates it and it is in scope. It must be
  acquirable, and a ``404`` on it later is an integrity failure, not a miss.
* ``DELETED`` — a previous capture enumerated it and this one does not. The
  publisher withdrew it. That is *missing*, and it is recorded as a tombstone
  rather than silently dropped, because a population that shrinks without saying
  so is indistinguishable from a broken capture.
* ``EXCLUDED`` — the publisher enumerates it and *we* declined it: an undated
  one-off export, a loader script, or a dataset outside the requested scope.
  That is *refused*, and it is our decision, recorded as ours.

The distinction matters because the two failure modes have opposite remedies. A
``DELETED`` item means the upstream population moved and our expectations should
follow. An ``EXCLUDED`` item means we chose, and the choice is reviewable. Only
``ACTIVE`` items make a promise that acquisition has to keep.

This module builds catalogs; it acquires nothing. Bytes are fetched by the
existing HTTPS content fetcher over the ``storage.courtlistener.com`` host, so
the transport's own refusal semantics (``IntegrityError`` for a candidate that
is gone or changed, a retryable ``ConnectionError`` for 429/5xx) apply
unchanged.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.errors import IntegrityError, LimitExceededError

CAPTURE_FORMAT = "docspec-courtlistener-bulk-capture"
CAPTURE_FORMAT_VERSION = "1.0"
CAPTURE_IDENTITY_KIND = "courtlistener-bulk-capture"

#: Public read host for the dumps. Acquisition must allow exactly this host.
CONTENT_HOST = "storage.courtlistener.com"
CONTENT_BASE = f"https://{CONTENT_HOST}"

#: The bucket's list API host. Captured, never fetched from during acquisition.
LISTING_HOST = "com-courtlistener-storage.s3.amazonaws.com"
LISTING_PREFIX = "bulk-data/"

_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

MAX_PINS_BYTES = 1024**2
MAX_CAPTURE_PAGE_BYTES = 8 * 1024**2

_PINS_KEYS = frozenset({"format", "formatVersion", "pinsId", "members", "origin"})
_MEMBER_KEYS = frozenset({"path", "mediaType", "byteSize", "digest"})

_MEDIA_TYPES = {
    ".csv.bz2": "application/x-bzip2",
    ".csv": "text/csv",
    ".sql": "application/sql",
    ".sh": "application/x-sh",
    ".zip": "application/zip",
}
_SUFFIXES = tuple(sorted(_MEDIA_TYPES, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class BulkObject:
    """One object the publisher enumerates, with the bytes it promises."""

    key: str
    size: int
    etag: str
    last_modified: str

    def __post_init__(self) -> None:
        require_text(self.key, "bulk object key")
        require_text(self.etag, "bulk object etag")
        require_text(self.last_modified, "bulk object last modified")
        if self.size < 0:
            raise ValueError("bulk object size must be non-negative")

    @property
    def filename(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str | None:
        for suffix in _SUFFIXES:
            if self.filename.endswith(suffix):
                return suffix
        return None

    @property
    def media_type(self) -> str:
        return _MEDIA_TYPES.get(self.suffix or "", "application/octet-stream")

    @property
    def stem(self) -> str:
        suffix = self.suffix
        return self.filename[: -len(suffix)] if suffix else self.filename

    @property
    def dump_date(self) -> date | None:
        """The dump's date stamp, or None for the publisher's undated exports."""
        tail = self.stem[-10:]
        try:
            return date.fromisoformat(tail)
        except ValueError:
            return None

    @property
    def dataset(self) -> str:
        """Dataset name with any date stamp removed (``opinions``, ``courts``...)."""
        stem = self.stem
        if self.dump_date is not None and len(stem) > 11 and stem[-11] == "-":
            return stem[:-11]
        return stem

    @property
    def locator(self) -> str:
        return f"{CONTENT_BASE}/{self.key}"

    @property
    def transport_version(self) -> str:
        """Pin the exact revision the publisher is offering right now.

        The listing's ETag is not a whole-object digest for multipart uploads, so
        it cannot stand in for a SHA-256. Combined with size and last-modified it
        still changes whenever the object does, which is what a version needs to
        do.
        """
        return f"s3-listing:{self.etag}:{self.size}:{self.last_modified}"


def _text(node: ET.Element, tag: str, label: str) -> str:
    value = node.findtext(f"s3:{tag}", None, _S3_NS)
    if value is None or not value.strip():
        raise IntegrityError(f"bulk listing entry is missing {label}")
    return value.strip()


def parse_listing_page(payload: bytes) -> tuple[tuple[BulkObject, ...], str | None]:
    """Parse one captured S3 listing page into objects plus its continuation token.

    Strict on purpose: a listing that does not name the bucket and prefix we
    captured, or an entry missing a size or version marker, is refused rather
    than partially believed. A truncated population is the one failure this whole
    module exists to make impossible to mistake for a complete one.
    """
    if len(payload) > MAX_CAPTURE_PAGE_BYTES:
        raise LimitExceededError(f"bulk listing page exceeds the {MAX_CAPTURE_PAGE_BYTES}-byte limit")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise IntegrityError("bulk listing page is not well-formed XML") from error

    prefix = root.findtext("s3:Prefix", "", _S3_NS)
    if prefix != LISTING_PREFIX:
        raise IntegrityError(f"bulk listing page covers prefix {prefix!r}, not {LISTING_PREFIX!r}")

    objects = []
    for node in root.findall("s3:Contents", _S3_NS):
        key = _text(node, "Key", "a key")
        if not key.startswith(LISTING_PREFIX):
            raise IntegrityError(f"bulk listing entry escapes the captured prefix: {key}")
        objects.append(
            BulkObject(
                key=key,
                size=int(_text(node, "Size", "a size")),
                etag=_text(node, "ETag", "an ETag").strip('"'),
                last_modified=_text(node, "LastModified", "a last-modified stamp"),
            )
        )

    truncated = root.findtext("s3:IsTruncated", "false", _S3_NS) == "true"
    token = root.findtext("s3:NextContinuationToken", None, _S3_NS)
    if truncated and not token:
        raise IntegrityError("bulk listing page is truncated but names no continuation token")
    return tuple(objects), (token if truncated else None)


def parse_capture(pages: Sequence[bytes]) -> tuple[BulkObject, ...]:
    """Parse an ordered capture of listing pages into one complete population.

    Refuses a capture whose last page is still truncated: that is a population we
    only partly saw, and admitting it would let a coverage denominator be quietly
    too small.
    """
    if not pages:
        raise IntegrityError("bulk capture contains no listing pages")
    seen: dict[str, BulkObject] = {}
    token: str | None = None
    for index, payload in enumerate(pages):
        objects, next_token = parse_listing_page(payload)
        for obj in objects:
            if obj.key in seen:
                raise IntegrityError(f"bulk capture enumerates {obj.key} more than once")
            seen[obj.key] = obj
        if index < len(pages) - 1 and next_token is None:
            raise IntegrityError("bulk capture has pages after an unterminated listing")
        token = next_token
    if token is not None:
        raise IntegrityError("bulk capture ends on a truncated listing page")
    return tuple(sorted(seen.values(), key=lambda o: o.key))


@dataclass(frozen=True, slots=True)
class BulkCapture:
    """One admitted capture: the publisher's enumeration plus its pinned identity."""

    capture_id: str
    objects: tuple[BulkObject, ...]
    origin: Mapping[str, Any]

    @property
    def byte_total(self) -> int:
        return sum(obj.size for obj in self.objects)

    def datasets(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for obj in self.objects:
            counts[obj.dataset] = counts.get(obj.dataset, 0) + 1
        return dict(sorted(counts.items()))


def _regular_file(path: Path, label: str) -> Path:
    resolved = Path(path)
    if resolved.is_symlink() or not resolved.is_file():
        raise IntegrityError(f"{label} is not a regular file")
    return resolved


def _closed_mapping(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise IntegrityError(f"{label} has an invalid closed shape")
    return value


def load_capture(pins_path: Path) -> BulkCapture:
    """Admit one pinned capture, refusing any byte that differs from its pin.

    The pins file is the only thing named by path; the listing pages are resolved
    below it, size-checked, and re-digested before a parser sees them. A capture
    edited after the fact fails here rather than changing a population downstream.
    """
    path = _regular_file(Path(pins_path).resolve(strict=True), "bulk capture pins file")
    directory = path.parent
    payload = path.read_bytes()
    if len(payload) > MAX_PINS_BYTES:
        raise LimitExceededError(f"bulk capture pins file exceeds the {MAX_PINS_BYTES}-byte limit")

    document = thaw_json(parse_canonical_json(payload, label="bulk capture pins file"))
    pins = _closed_mapping(document, _PINS_KEYS, "bulk capture pins file")
    if pins["format"] != CAPTURE_FORMAT or pins["formatVersion"] != CAPTURE_FORMAT_VERSION:
        raise IntegrityError("bulk capture pins file has an unknown format")

    content = {name: value for name, value in pins.items() if name != "pinsId"}
    if pins["pinsId"] != stable_urn(CAPTURE_IDENTITY_KIND, content):
        raise IntegrityError("bulk capture identity differs from its canonical content")

    members = pins["members"]
    if not isinstance(members, list) or not members:
        raise IntegrityError("bulk capture pins file declares no listing pages")

    pages: list[bytes] = []
    for member in members:
        member = _closed_mapping(member, _MEMBER_KEYS, "bulk capture member")
        relative = require_text(member["path"], "bulk capture member path")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise IntegrityError(f"bulk capture member path escapes the capture: {relative}")
        member_path = _regular_file(directory / relative, f"bulk capture member {relative}")
        data = member_path.read_bytes()
        if len(data) != member["byteSize"]:
            raise IntegrityError(f"bulk capture member differs in size from its pin: {relative}")
        if sha256_digest(data) != member["digest"]:
            raise IntegrityError(f"bulk capture member differs from its pinned digest: {relative}")
        pages.append(data)

    return BulkCapture(
        capture_id=pins["pinsId"],
        objects=parse_capture(pages),
        origin=pins["origin"],
    )


def write_capture_pins(
    directory: Path,
    *,
    page_paths: Sequence[Path],
    origin: Mapping[str, Any],
) -> Path:
    """Pin a freshly captured listing so later runs admit exactly these bytes."""
    members = []
    for page in page_paths:
        data = _regular_file(Path(page), f"capture page {page}").read_bytes()
        members.append(
            {
                "byteSize": len(data),
                "digest": sha256_digest(data),
                "mediaType": "application/xml",
                "path": Path(page).relative_to(directory).as_posix(),
            }
        )
    content = {
        "format": CAPTURE_FORMAT,
        "formatVersion": CAPTURE_FORMAT_VERSION,
        "members": members,
        "origin": dict(origin),
    }
    pins = {**content, "pinsId": stable_urn(CAPTURE_IDENTITY_KIND, content)}
    path = Path(directory) / "pins.json"
    path.write_bytes(canonical_json_file_bytes(pins))
    return path


def build_source_items(
    capture: BulkCapture,
    *,
    datasets: Iterable[str] | None = None,
    previous: BulkCapture | None = None,
) -> list[SourceItem]:
    """Turn one capture into the population a catalog publishes.

    ``datasets`` narrows scope; anything outside it is ``EXCLUDED`` — refused by
    us, and recorded as such rather than omitted. Objects the previous capture
    enumerated and this one does not become ``DELETED`` tombstones: the publisher
    withdrew them, which is missing, not refused.
    """
    wanted = None if datasets is None else set(datasets)
    items: list[SourceItem] = []

    for obj in capture.objects:
        if obj.dump_date is None:
            state = SourceItemState.EXCLUDED
            reason = "undated one-off export, not a periodic dump"
        elif wanted is not None and obj.dataset not in wanted:
            state = SourceItemState.EXCLUDED
            reason = "dataset outside the requested scope"
        else:
            state = SourceItemState.ACTIVE
            reason = None

        candidates: tuple[CandidateFile, ...] = ()
        if state is SourceItemState.ACTIVE:
            candidates = (
                CandidateFile(
                    candidate_id="dump",
                    locator=obj.locator,
                    media_type=obj.media_type,
                    expected_size=obj.size,
                    transport_version=obj.transport_version,
                ),
            )
        items.append(
            SourceItem(
                item_id=obj.key,
                version=obj.transport_version,
                candidates=candidates,
                state=state,
                metadata={
                    "dataset": obj.dataset,
                    "dumpDate": obj.dump_date.isoformat() if obj.dump_date else None,
                    "byteSize": obj.size,
                    **({"exclusionReason": reason} if reason else {}),
                },
            )
        )

    if previous is not None:
        current_keys = {obj.key for obj in capture.objects}
        for obj in previous.objects:
            if obj.key in current_keys:
                continue
            items.append(
                SourceItem(
                    item_id=obj.key,
                    version=obj.transport_version,
                    candidates=(),
                    state=SourceItemState.DELETED,
                    metadata={
                        "dataset": obj.dataset,
                        "dumpDate": obj.dump_date.isoformat() if obj.dump_date else None,
                        "byteSize": obj.size,
                        "withdrawnFromCapture": capture.capture_id,
                    },
                )
            )

    items.sort(key=lambda item: (item.item_id, item.version))
    return items


def coverage_for(capture: BulkCapture, items: Sequence[SourceItem]) -> dict[str, Any]:
    """The numbers a coverage check is measured against, carried on the catalog."""
    active = [i for i in items if i.state is SourceItemState.ACTIVE]
    return {
        "captureId": capture.capture_id,
        "listingHost": LISTING_HOST,
        "listingPrefix": LISTING_PREFIX,
        "publisherObjectCount": len(capture.objects),
        "publisherByteTotal": capture.byte_total,
        "datasetCounts": capture.datasets(),
        "activeItemCount": len(active),
        "activeByteTotal": sum(int(i.metadata["byteSize"]) for i in active),
    }


def build_catalog(
    catalog: Any,
    capture: BulkCapture,
    *,
    datasets: Iterable[str] | None = None,
    previous: BulkCapture | None = None,
) -> Any:
    """Publish one capture as a source catalog a future campaign can run over."""
    items = build_source_items(capture, datasets=datasets, previous=previous)
    return catalog.write(items, coverage=coverage_for(capture, items))


def capture_digest_of(pins_path: Path) -> str:
    """Digest of the pins file itself, for receipts that must name this capture."""
    return sha256_digest(_regular_file(Path(pins_path), "bulk capture pins file").read_bytes())
