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

Absent corpus
-------------
The corpus is a local salvage checkpoint, so most machines do not have it.
`corpus_root` returns `None` rather than raising when it is absent, and
`DOCSPEC_QUALIFICATION_CORPUS` relocates it, so tests skip instead of failing
and a relocated checkout still mints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
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
        "runs",
        "sourceCatalog",
        "tier",
    }
)
_MEMBER_KEYS = frozenset({"byteSize", "digest", "mediaType", "path", "role"})
_CATALOG_KEYS = frozenset({"catalogId", "digest", "locator"})


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


def load_pin(pin_path: Path = PIN_PATH, *, root: Path | None = None) -> PinnedCorpus:
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
    )


def write_pin(root: Path, *, pin_path: Path = PIN_PATH) -> Path:
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
    content = {
        "campaignId": catalog_set["campaignId"],
        "drawDigests": catalog_set["inputs"],
        "format": PIN_FORMAT,
        "formatVersion": PIN_FORMAT_VERSION,
        "members": sorted(members, key=lambda member: member["path"]),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="mint the pin from the corpus root")
    parser.add_argument("--root", type=Path, default=None, help="corpus root override")
    args = parser.parse_args(argv)

    root = args.root or corpus_root()
    if root is None:
        print(f"the pinned qualification corpus is absent; set {CORPUS_ROOT_VARIABLE} to relocate it")
        return 1
    if args.write:
        print(f"wrote {write_pin(root)}")
        return 0
    pinned = load_pin(root=root)
    print(f"{pinned.pins_id}\n  catalog {pinned.catalog_id}\n  digest  sha256:{pinned.catalog_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
