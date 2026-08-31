#!/usr/bin/env python3
"""The pinned 10k qualification corpus the first real DocumentRelease is minted from.

`docs/decisions/0001-document-release-2-0.md` D1 fixes the input: the FULL tier
of the `fr-mirrulations-10k-v1` checkpoint, one `SourceCatalog` snapshot,
10,000 items, named by `catalogId` and by the BYTE digest of its `catalog.json`.
This module is the reproducible citation of that decision -- the corpus root,
the three pinned files, and the two upstream draw digests the checkpoint's own
`catalog-set.json` records -- so a mint says exactly which bytes it read and a
later run over different bytes fails here rather than downstream.

Why a pin file rather than constants
------------------------------------
The corpus is 545 MB of content-addressed blobs outside this repository. It
cannot be committed, so what is committed is the pin: a closed, canonical
document whose own identity is minted over its content, following the house
pattern already in `tools/courtlistener_bulk_source.py`
(`fixtures/courtlistener-bulk-v1/pins.json`). `load_pin` re-reads every pinned
file and refuses a byte that differs, exactly as `load_capture` does; nothing
downstream sees a path it did not verify first.

The rescue map (amendment C2)
-----------------------------
The salvage brought the blob stores across and left behind some of the record
layer that points into them: for 194 source items the checkpoint HOLDS the bytes
and no record names them, so the first two mints refused them for
`capture.no-preserved-copy` -- an index gap reported as an absent document. The
pin therefore carries a fourth digest-pinned input, the campaign's own capture
store map, and `rescued_captures` reads it as a SECOND POINTER into the same
store. It is not a second source of bytes: a map row naming a blob the checkpoint
does not hold supplies nothing, and every rescued copy goes through the same
mandatory size-and-digest check as any other. "Adopt and verify, preserved-copy
is rung one" is unchanged; what changed is that the builder now has the index.

Absent corpus
-------------
The corpus is a local salvage checkpoint, so most machines do not have it.
`corpus_root` returns `None` rather than raising when it is absent, and
`DOCSPEC_QUALIFICATION_CORPUS` relocates it, so tests skip instead of failing
and a relocated checkout still mints. `rescue_root` and
`DOCSPEC_QUALIFICATION_RESCUE` are the same affordance for the rescue map --
but `load_pin` REFUSES when the pin declares a rescue map it cannot read, rather
than continuing without it. A mint that quietly dropped 193 documents because a
file was missing would produce a different release under the same procedure, and
that is precisely the silent difference this pin exists to prevent.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    require_text,
    stable_urn,
    thaw_json,
)
from docspec.errors import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = REPO_ROOT / "fixtures" / "fr-mirrulations-10k-v1" / "pins.json"

PIN_FORMAT = "docspec-qualification-corpus-pin"
PIN_FORMAT_VERSION = "1.0"
PIN_IDENTITY_KIND = "qualification-corpus-pin"

CAMPAIGN_ID = "fr-mirrulations-10k-v1"
TIER = "full"

# Where the salvage checkpoint lives on the machine that minted from it. An
# environment variable relocates it; nothing else in this repository names it.
CORPUS_ROOT_VARIABLE = "DOCSPEC_QUALIFICATION_CORPUS"
DEFAULT_CORPUS_ROOT = (
    Path.home()
    / "Work"
    / "corpora"
    / "_salvage-2026-08-28"
    / "docspec-qualification"
    / CAMPAIGN_ID
)

# Where the rescue map lives. It is a SECOND POINTER into the blob store the
# corpus root already holds, not a second store: what came across in the salvage
# is the blobs, and what did not is the record-layer rows that name 387 of them.
# Amendment C2 pins it here rather than naming a path in the builder, so a mint
# says which map it read and a map edited after the fact fails at the pin.
RESCUE_ROOT_VARIABLE = "DOCSPEC_QUALIFICATION_RESCUE"
DEFAULT_RESCUE_ROOT = (
    Path.home() / "Work" / "corpora" / "_rescue-2026-08-31-qualification-store-map"
)

# The largest pin document this loader will read, mirroring the bulk-capture
# loader's own bound: a pins file is a handful of digests, never a payload.
MAX_PIN_BYTES = 64 * 1024

_PIN_KEYS = frozenset(
    {
        "campaignId",
        "drawDigests",
        "format",
        "formatVersion",
        "members",
        "pinsId",
        "rescueMap",
        "runs",
        "sourceCatalog",
        "tier",
    }
)
_MEMBER_KEYS = frozenset({"byteSize", "digest", "mediaType", "path", "role"})
_CATALOG_KEYS = frozenset({"catalogId", "digest", "locator"})
_RESCUE_MAP_KEYS = frozenset({"byteSize", "digest", "mediaType", "path", "role"})

# The rescue map's own file name, under the rescue root.
RESCUE_MAP_FILE = "full-store-map-v2.jsonl.gz"
RESCUE_MAP_ROLE = "capture-store-map"
RESCUE_MAP_MEDIA_TYPE = "application/gzip"


class QualificationCorpusError(IntegrityError):
    """The pinned corpus is absent, incomplete, or differs from its pin."""


@dataclass(frozen=True, slots=True)
class PinnedCorpus:
    """One verified qualification corpus: its root, its pin, and its catalog bytes."""

    pins_id: str
    root: Path
    campaign_id: str
    tier: str
    catalog_id: str
    catalog_digest: str
    catalog_locator: str
    catalog_bytes: bytes
    items_path: Path
    draw_digests: Mapping[str, Any]
    rescue_map_path: Path
    rescue_map_digest: str

    @property
    def run_roots(self) -> tuple[Path, ...]:
        """Every run store whose preserved copies this mint may adopt from."""

        return tuple(self.root / "runs" / name for name in RUN_NAMES)


# The run stores the builder adopts preserved copies from, in the order it
# prefers them. The full tier's own run is first; the two smaller runs are read
# because they hold preserved copies of items the full run never captured, and
# a preserved copy is rung one whichever run preserved it.
RUN_NAMES: tuple[str, ...] = ("full", "intermediate", "smoke")


def corpus_root() -> Path | None:
    """The pinned corpus root, or ``None`` when this machine does not have it."""

    override = os.environ.get(CORPUS_ROOT_VARIABLE)
    candidate = Path(override) if override else DEFAULT_CORPUS_ROOT
    return candidate if candidate.is_dir() else None


def rescue_root() -> Path | None:
    """The rescue map's root, or ``None`` when this machine does not have it."""

    override = os.environ.get(RESCUE_ROOT_VARIABLE)
    candidate = Path(override) if override else DEFAULT_RESCUE_ROOT
    return candidate if candidate.is_dir() else None


def _file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise QualificationCorpusError(f"{label} is absent: {path}")
    return path


def _closed(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise QualificationCorpusError(f"{label} has an invalid closed shape")
    return value


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_pin(
    pin_path: Path = PIN_PATH,
    *,
    root: Path | None = None,
    rescue: Path | None = None,
) -> PinnedCorpus:
    """Admit the pinned corpus, refusing any byte that differs from its pin.

    The pin is the only thing named by path. Every member below it is resolved
    against the corpus root, size-checked, and re-digested before any caller
    sees it, so a checkpoint edited after the fact fails here rather than
    changing a release downstream.
    """

    path = _file(Path(pin_path), "qualification corpus pin")
    payload = path.read_bytes()
    if len(payload) > MAX_PIN_BYTES:
        raise QualificationCorpusError(f"corpus pin exceeds the {MAX_PIN_BYTES}-byte limit")
    document = thaw_json(parse_canonical_json(payload, label="qualification corpus pin"))
    pin = _closed(document, _PIN_KEYS, "qualification corpus pin")
    if pin["format"] != PIN_FORMAT or pin["formatVersion"] != PIN_FORMAT_VERSION:
        raise QualificationCorpusError("qualification corpus pin has an unknown format")
    content = {name: value for name, value in pin.items() if name != "pinsId"}
    if pin["pinsId"] != stable_urn(PIN_IDENTITY_KIND, content):
        raise QualificationCorpusError("corpus pin identity differs from its canonical content")

    resolved = Path(root) if root is not None else corpus_root()
    if resolved is None:
        raise QualificationCorpusError(
            f"the pinned qualification corpus is absent; set {CORPUS_ROOT_VARIABLE} to relocate it"
        )

    catalog = _closed(pin["sourceCatalog"], _CATALOG_KEYS, "corpus pin sourceCatalog")
    members = pin["members"]
    if not isinstance(members, list) or not members:
        raise QualificationCorpusError("corpus pin declares no members")

    bytes_by_role: dict[str, bytes] = {}
    paths_by_role: dict[str, Path] = {}
    for raw in members:
        member = _closed(raw, _MEMBER_KEYS, "corpus pin member")
        relative = require_text(member["path"], "corpus pin member path")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise QualificationCorpusError(f"corpus pin member escapes the corpus: {relative}")
        member_path = _file(resolved / relative, f"corpus pin member {relative}")
        if member_path.stat().st_size != member["byteSize"]:
            raise QualificationCorpusError(f"corpus member differs in size from its pin: {relative}")
        # `items.jsonl` is 17 MB; digest it streaming rather than holding two
        # copies, and keep the bytes only for the members a caller reads whole.
        digest = hashlib.sha256()
        with member_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != member["digest"]:
            raise QualificationCorpusError(f"corpus member differs from its pinned digest: {relative}")
        paths_by_role[member["role"]] = member_path
        if member["role"] != "catalog-items":
            bytes_by_role[member["role"]] = member_path.read_bytes()

    for role in ("catalog-set", "source-catalog", "catalog-items"):
        if role not in paths_by_role:
            raise QualificationCorpusError(f"corpus pin declares no {role!r} member")

    catalog_bytes = bytes_by_role["source-catalog"]
    if _digest(catalog_bytes) != catalog["digest"].removeprefix("sha256:"):
        raise QualificationCorpusError("the pinned catalog digest does not describe the pinned bytes")
    snapshot = json.loads(catalog_bytes.decode("utf-8"))
    if snapshot.get("catalogId") != catalog["catalogId"]:
        raise QualificationCorpusError("the pinned catalog declares a different catalogId")

    # The rescue map (amendment C2). Resolved against its own root because it is
    # a different tree from the checkpoint, and verified the same way every other
    # pinned byte is. Absent is a REFUSAL rather than a shrug: a mint that
    # quietly dropped 193 documents because a file was missing would produce a
    # different release under the same procedure, which is the class of silent
    # difference this pin exists to prevent.
    rescue_declared = _closed(pin["rescueMap"], _RESCUE_MAP_KEYS, "corpus pin rescueMap")
    rescue_relative = require_text(rescue_declared["path"], "corpus pin rescueMap path")
    if rescue_relative.startswith("/") or ".." in Path(rescue_relative).parts:
        raise QualificationCorpusError(f"corpus pin rescue map escapes its root: {rescue_relative}")
    rescue_resolved = Path(rescue) if rescue is not None else rescue_root()
    if rescue_resolved is None:
        raise QualificationCorpusError(
            f"the pinned rescue map is absent; set {RESCUE_ROOT_VARIABLE} to relocate it"
        )
    rescue_path = _file(rescue_resolved / rescue_relative, f"corpus pin rescue map {rescue_relative}")
    if rescue_path.stat().st_size != rescue_declared["byteSize"]:
        raise QualificationCorpusError("the pinned rescue map differs in size from its pin")
    rescue_digest = _digest(rescue_path.read_bytes())
    if rescue_digest != rescue_declared["digest"].removeprefix("sha256:"):
        raise QualificationCorpusError("the pinned rescue map differs from its pinned digest")

    return PinnedCorpus(
        pins_id=pin["pinsId"],
        root=resolved,
        campaign_id=pin["campaignId"],
        tier=pin["tier"],
        catalog_id=catalog["catalogId"],
        catalog_digest=catalog["digest"].removeprefix("sha256:"),
        catalog_locator=catalog["locator"],
        catalog_bytes=catalog_bytes,
        items_path=paths_by_role["catalog-items"],
        draw_digests=pin["drawDigests"],
        rescue_map_path=rescue_path,
        rescue_map_digest=rescue_digest,
    )


def write_pin(root: Path, *, rescue: Path | None = None, pin_path: Path = PIN_PATH) -> Path:
    """Mint the pin from a corpus root, reading every value off the checkpoint.

    Nothing here is written down twice: the catalog identity, its digest, its
    locator, and both upstream draw digests are read from the checkpoint's own
    `catalog-set.json`, and every member digest is taken over the bytes.
    """

    root = Path(root)
    catalog_set_path = _file(root / "catalog-set.json", "checkpoint catalog set")
    catalog_set = json.loads(catalog_set_path.read_text(encoding="utf-8"))
    tier = catalog_set["tiers"][TIER]["sourceCatalog"]
    catalog_path = _file(root / "source-catalogs" / tier["locator"], "pinned source catalog")
    snapshot = json.loads(catalog_path.read_text(encoding="utf-8"))
    items_path = _file(catalog_path.parent / snapshot["itemsMember"]["path"], "pinned catalog items")

    members = []
    for role, member_path, media_type in (
        ("catalog-set", catalog_set_path, "application/json"),
        ("source-catalog", catalog_path, "application/json"),
        ("catalog-items", items_path, "application/x-ndjson"),
    ):
        members.append(
            {
                "byteSize": member_path.stat().st_size,
                "digest": _digest(member_path.read_bytes()),
                "mediaType": media_type,
                "path": member_path.relative_to(root).as_posix(),
                "role": role,
            }
        )
    rescue_resolved = Path(rescue) if rescue is not None else rescue_root()
    if rescue_resolved is None:
        raise QualificationCorpusError(
            f"the rescue map is absent; set {RESCUE_ROOT_VARIABLE} to relocate it"
        )
    rescue_path = _file(rescue_resolved / RESCUE_MAP_FILE, "rescue map")
    rescue_bytes = rescue_path.read_bytes()
    content = {
        "campaignId": catalog_set["campaignId"],
        "drawDigests": catalog_set["inputs"],
        "format": PIN_FORMAT,
        "formatVersion": PIN_FORMAT_VERSION,
        "members": sorted(members, key=lambda member: member["path"]),
        "rescueMap": {
            "byteSize": len(rescue_bytes),
            "digest": f"sha256:{_digest(rescue_bytes)}",
            "mediaType": RESCUE_MAP_MEDIA_TYPE,
            "path": RESCUE_MAP_FILE,
            "role": RESCUE_MAP_ROLE,
        },
        "runs": list(RUN_NAMES),
        "sourceCatalog": {
            "catalogId": tier["catalogId"],
            "digest": tier["digest"],
            "locator": "source-catalogs/" + tier["locator"],
        },
        "tier": TIER,
    }
    pin = {**content, "pinsId": stable_urn(PIN_IDENTITY_KIND, content)}
    pin_path = Path(pin_path)
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_bytes(canonical_json_file_bytes(pin))
    return pin_path


# ─── Preserved copies ──────────────────────────────────────────────────
#
# The checkpoint's blob stores are content-addressed and carry no index of
# their own; what says which bytes belong to which source item is the run's
# record layer, `docspec-file-record/1.0`, written by the acquisition that
# preserved them. Reading it is how "preserved-copy is rung one" is honoured
# without a single request: the record names the blob, the blob store is
# addressed by digest, and the builder re-digests the bytes before adopting
# them.

FILE_RECORD_SCHEMA = "docspec-file-record/1.0"

# Which index named a preserved copy. Both point into the SAME pinned blob store
# -- "adopt and verify, preserved-copy is rung one" is unchanged -- and the
# distinction is carried rather than dropped so a mint can say how many of its
# captures it found only because the rescue map named them (amendment C2).
CHECKPOINT_RECORDS = "checkpoint-record-layer"
RESCUE_MAP = "rescue-map"


@dataclass(frozen=True, slots=True)
class PreservedCapture:
    """One preserved rendition: which item it belongs to, and where its bytes are."""

    source_item_id: str
    candidate_id: str
    media_type: str
    digest: str
    byte_size: int
    path: Path
    acquired_at: str
    acquisition_started_at: str | None
    run: str
    origin: str = CHECKPOINT_RECORDS

    def read(self) -> bytes:
        """The preserved bytes, refusing anything that is not what was recorded."""

        if not self.path.is_file():
            raise QualificationCorpusError(f"preserved blob is absent: {self.path}")
        payload = self.path.read_bytes()
        if len(payload) != self.byte_size:
            raise QualificationCorpusError(f"preserved blob differs in size from its record: {self.path}")
        if _digest(payload) != self.digest.removeprefix("sha256:"):
            raise QualificationCorpusError(f"preserved blob differs from its recorded digest: {self.path}")
        return payload


def _file_records(run_root: Path) -> Iterator[dict[str, Any]]:
    records = run_root / "records"
    layers = records / "record-layers"
    if not layers.is_dir():
        return
    for layer_path in sorted(layers.rglob("*.json")):
        layer = json.loads(layer_path.read_text(encoding="utf-8"))
        if layer.get("schema", {}).get("schemaId") != FILE_RECORD_SCHEMA:
            continue
        for member in layer.get("members", ()):
            member_path = records / member["path"]
            if not member_path.is_file():
                raise QualificationCorpusError(f"record member is absent: {member_path}")
            with member_path.open(encoding="utf-8") as handle:
                for line in handle:
                    yield json.loads(line)["payload"]


def preserved_captures(pinned: PinnedCorpus) -> dict[str, dict[str, PreservedCapture]]:
    """Every preserved rendition in the checkpoint, by source item and candidate.

    The runs are read in `RUN_NAMES` order and the first run to preserve a
    candidate wins, so the full tier's own run is preferred and the smaller runs
    only supply what it never captured. Which run a copy came from is carried on
    the record rather than dropped: a release that adopted a copy from another
    run must be able to say so.
    """

    found: dict[str, dict[str, PreservedCapture]] = {}
    for name in RUN_NAMES:
        run_root = pinned.root / "runs" / name
        for record in _file_records(run_root):
            blob = record["blob"]
            item = found.setdefault(record["sourceItemId"], {})
            if record["candidateId"] in item:
                continue
            item[record["candidateId"]] = PreservedCapture(
                source_item_id=record["sourceItemId"],
                candidate_id=record["candidateId"],
                media_type=record["mediaType"],
                digest=blob["digest"],
                byte_size=blob["byteSize"],
                path=run_root / "blobs" / blob["locator"],
                acquired_at=record["acquiredAt"],
                acquisition_started_at=record.get("acquisitionStartedAt"),
                run=name,
            )
    return found


def rescued_captures(
    pinned: PinnedCorpus, preserved: Mapping[str, Mapping[str, PreservedCapture]]
) -> dict[str, dict[str, PreservedCapture]]:
    """Preserved copies the checkpoint HOLDS and its record layer never named.

    Amendment C2. The salvage brought the blobs across and left behind the
    record-layer rows that point at 387 of them, so the builder asked the only
    index it had, was told nothing, and refused 194 items for `no-preserved-copy`
    -- an index gap reported as an absent document. The rescue map is a second
    pointer into the SAME store, and three rules keep it a rescue rather than a
    fetch:

    1. it is consulted only where ``preserved`` has no pointer for that
       ``(sourceItemId, candidateId)``, so it can never override the
       checkpoint's own record;
    2. the blob must ALREADY be in the pinned checkpoint. A row naming bytes the
       checkpoint does not hold supplies nothing and is dropped here -- this
       makes no request, and a rescue that had to reach for bytes would be a
       fetch wearing another name;
    3. the returned captures are ordinary `PreservedCapture`s, so the caller
       reads them through the same mandatory size-and-digest check every
       preserved copy goes through, and a mismatch is a capture failure.

    The map records no acquisition START instant, so `acquisition_started_at` is
    null rather than borrowed from anywhere. The `acquiredAt` it does record is
    the campaign's own, the same value all 9,774 checkpoint records carry.
    """

    found: dict[str, dict[str, PreservedCapture]] = {}
    with gzip.open(pinned.rescue_map_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            run = row.get("run")
            if run not in RUN_NAMES:
                continue
            run_root = pinned.root / "runs" / run
            for captured in row.get("capturedFiles", ()):
                if captured.get("disposition") != "captured":
                    continue
                item_id = captured["sourceItemId"]
                candidate_id = captured["candidateId"]
                if candidate_id in preserved.get(item_id, {}):
                    continue
                if candidate_id in found.get(item_id, {}):
                    continue
                blob = captured["blob"]
                path = run_root / "blobs" / blob["locator"]
                if not path.is_file():
                    continue
                found.setdefault(item_id, {})[candidate_id] = PreservedCapture(
                    source_item_id=item_id,
                    candidate_id=candidate_id,
                    media_type=captured["mediaType"],
                    digest=blob["digest"],
                    byte_size=blob["byteSize"],
                    path=path,
                    acquired_at=captured["acquiredAt"],
                    acquisition_started_at=None,
                    run=run,
                    origin=RESCUE_MAP,
                )
    return found


def catalog_items(pinned: PinnedCorpus) -> Iterator[dict[str, Any]]:
    """The pinned catalog's items, streamed in the order the snapshot wrote them."""

    with pinned.items_path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="mint the pin from the corpus root")
    parser.add_argument("--root", type=Path, default=None, help="corpus root override")
    parser.add_argument("--rescue", type=Path, default=None, help="rescue map root override")
    args = parser.parse_args(argv)

    root = args.root or corpus_root()
    if root is None:
        print(f"the pinned qualification corpus is absent; set {CORPUS_ROOT_VARIABLE} to relocate it")
        return 1
    if args.write:
        print(f"wrote {write_pin(root, rescue=args.rescue)}")
        return 0
    pinned = load_pin(root=root, rescue=args.rescue)
    print(
        f"{pinned.pins_id}\n  catalog    {pinned.catalog_id}"
        f"\n  digest     sha256:{pinned.catalog_digest}"
        f"\n  rescue map sha256:{pinned.rescue_map_digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
