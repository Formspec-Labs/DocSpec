"""Map pinned CourtListener listing pages into DocSpec dataset selections.

SpicyDocs owns listing grammar, filenames, URLs and exact source revision markers.
This tool owns captured-input pins, page-set consistency, dataset scope and
coverage accounting. ACTIVE means selected from this listing; EXCLUDED records
our selection decision. DELETED records absence from a later captured listing,
not independent proof that an object is unavailable at the publisher.

The listing's ETag, size and timestamp describe an observed revision. They do not
hash document bytes or establish that a later download still matches. Acquisition
uses the injected HTTPS fetcher and retains its own byte evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from spicy_docs.sources.courtlistener_listing import (
    BULK_LIST_URL,
    BULK_PREFIX,
    BulkObject,
    parse_listing_page,
)

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

MAX_PINS_BYTES = 1024**2

_PINS_KEYS = frozenset({"format", "formatVersion", "pinsId", "members", "origin"})
_MEMBER_KEYS = frozenset({"path", "mediaType", "byteSize", "digest"})


def parse_capture(pages: Sequence[bytes]) -> tuple[BulkObject, ...]:
    """Read pinned pages, rejecting repeated keys and inconsistent termination.

    These checks establish what the supplied pages enumerate. They cannot prove
    that the capture process retained every intermediate publisher response.
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
            raise IntegrityError("bulk capture has pages after a completed listing page")
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
            dataset = obj.dataset or ""
            counts[dataset] = counts.get(dataset, 0) + 1
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
    enumerated and this one does not become ``DELETED`` tombstones. This records
    absence from the later listing separately from our selection decision.
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
                    locator=obj.url,
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
        "listingHost": urlsplit(BULK_LIST_URL).hostname,
        "listingPrefix": BULK_PREFIX,
        "publisherObjectCount": len(capture.objects),
        "publisherByteTotal": capture.byte_total,
        "datasetCounts": capture.datasets(),
        "activeItemCount": len(active),
        "activeByteTotal": sum(int(i.metadata["byteSize"]) for i in active),
    }
