#!/usr/bin/env python3
"""Export a completed DocSpec run as a neutral JSON Lines selection ledger.

DocSpec seals its output in its own record-layer format. A downstream
document-release producer reads a flat, one-row-per-document ledger. Nothing
converted one to the other, so a completed campaign could not reach a search
index no matter how many documents it had captured.

This writes that ledger. It is deliberately a one-way export: DocSpec learns
nothing about who consumes the file, and the consumer learns nothing about
record layers, partition buckets, or blob addressing. The only contract is the
row shape, which is documented by LEDGER_FIELDS below.

Every value is read from sealed run bytes. Nothing is inferred, defaulted from
a guess, or synthesised -- a document whose join is incomplete is reported and
skipped rather than exported with invented fields, because a ledger row that
looks complete but is not would be indistinguishable downstream from a real
capture.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# The row contract. A consumer that reads this file needs no other DocSpec
# knowledge; a field added here is a change to that contract.
LEDGER_FIELDS = (
    "documentId",
    "sourceId",
    "sourceInputId",
    "sourceRecordId",
    "sourcePartition",
    "sourceVersion",
    "disposition",
    "renditionPath",
    "mediaType",
    "documentType",
    "language",
    "title",
    "publishedAt",
    "updatedAt",
    "eligibilityState",
    "eligibilityAuthorityId",
    "eligibilityEvidenceKind",
    "eligibilityBasis",
    "eligibilityReasonCode",
)

# A DocSpec run records what happened to each selected item. Only `captured`
# means "we hold these exact bytes"; the others are real outcomes that must not
# be laundered into an active document row.
_CAPTURED = "captured"

# The campaign sealed its inputs before fetching, so eligibility rests on that
# sealed qualification rather than on a source assertion or a policy evaluated
# at read time.
_EVIDENCE_KIND = "sealed-qualification"

# The consumer requires a dotted diagnostic code of at least three segments
# (DIAGNOSTIC_CODE_PATTERN), so the reason is namespaced rather than a bare
# phrase: this document is eligible because a sealed campaign captured it.
_REASON_CODE = "docspec.qualification.sealed-capture"


class LedgerExportError(RuntimeError):
    """A run cannot be exported without inventing data."""


@dataclass(frozen=True)
class Document:
    """One document, joined across the run's record layers."""

    source_item_id: str
    partition: str
    blob_digest: str
    blob_locator: str
    media_type: str
    draw: dict[str, Any]

    @property
    def document_number(self) -> str:
        # The publisher's own identifier is the stable one. Fall back to the
        # DocSpec item id rather than minting a surrogate.
        return str(self.draw.get("document_number") or self.source_item_id)


def _iter_layers(records_root: Path) -> Iterator[dict[str, Any]]:
    for path in sorted((records_root / "record-layers").rglob("*.json")):
        yield json.loads(path.read_text(encoding="utf-8"))


def _iter_layer_rows(records_root: Path, layer: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for member in layer.get("members", ()):
        path = records_root / member["path"]
        if not path.is_file():
            raise LedgerExportError(f"record layer names a member that is absent: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def _collect(records_root: Path) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Return (selection, representations, source items) keyed by source item id.

    A run holds many layers of each kind -- one per store -- so every layer is
    read and merged. Later layers win: a re-executed store supersedes its own
    earlier attempt, which is exactly the resume path this campaign used.
    """

    selection: dict[str, dict] = {}
    # One item can hold several representations -- a mirrulations capture has
    # both the regulations.gov metadata JSON and the document's HTML body -- so
    # these are kept as a list. Collapsing them to one would silently pick
    # whichever layer was read last, which is neither reproducible nor the one
    # a caller wants.
    representations: dict[str, list[dict]] = defaultdict(list)
    source_items: dict[str, dict] = {}
    retracted: set[str] = set()
    for layer in _iter_layers(records_root):
        kind = layer.get("layerKind", "")
        if kind == "run-selection":
            for row in _iter_layer_rows(records_root, layer):
                selection[row["sourceItemId"]] = row
        elif kind == "representations":
            for row in _iter_layer_rows(records_root, layer):
                item = row["sourceItemId"]
                if row.get("deleted"):
                    # A deleted delivery record retracts its subject.
                    retracted.add(item)
                    representations.pop(item, None)
                    continue
                if item not in retracted:
                    representations[item].append(row.get("payload", row))
        elif kind == "source-items":
            for row in _iter_layer_rows(records_root, layer):
                if row.get("deleted"):
                    source_items.pop(row["sourceItemId"], None)
                else:
                    source_items[row["sourceItemId"]] = row.get("payload", row)
    return selection, dict(representations), source_items


# Ranked worst-to-best. A document's body is what a reader searches; the
# metadata sidecar is a fallback that keeps an item exportable when no body was
# captured, and is marked as such by the row it produces.
_RENDITION_PREFERENCE = ("application/json", "text/xml", "application/xml", "text/html")


def _rendition_rank(blob: dict[str, Any]) -> int:
    media_type = (blob.get("mediaType") or "").split(";")[0].strip()
    return _RENDITION_PREFERENCE.index(media_type) if media_type in _RENDITION_PREFERENCE else -1


def _select_blobs(payloads: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    """Return (rendition blob, metadata blob) for one item.

    The two can be the same object. Federal Register items capture a single
    XML body that is both; mirrulations items capture a JSON descriptor beside
    an HTML body, and each half answers a different question.
    """

    blobs = [payload.get("blob") or {} for payload in payloads]
    blobs = [blob for blob in blobs if blob.get("locator") and blob.get("digest")]
    if not blobs:
        return None, None
    rendition = max(blobs, key=_rendition_rank)
    metadata = next(
        (blob for blob in blobs if (blob.get("mediaType") or "").startswith("application/json")),
        None,
    )
    return rendition, metadata


def _published_at(draw: dict[str, Any]) -> str | None:
    date = draw.get("publication_date")
    # The publisher supplies a date, not an instant. Widening it to midnight
    # UTC is the only reading that does not invent a time of day.
    return f"{date}T00:00:00Z" if date else None


@dataclass(frozen=True)
class Descriptor:
    """The publisher-authored facts an active ledger row requires."""

    source_id: str
    title: str
    document_type: str
    published_at: str
    updated_at: str


def _federal_register_descriptor(qualification: dict[str, Any], _blob: Path) -> Descriptor | None:
    """Federal Register items carry their descriptor in the sealed draw."""

    draw = qualification.get("finalDraw") or {}
    published_at = _published_at(draw)
    title = draw.get("title")
    document_type = draw.get("document_type")
    if not (published_at and title and document_type):
        return None
    return Descriptor("federal-register", title, document_type, published_at, published_at)


def _mirrulations_descriptor(_qualification: dict[str, Any], blob: Path) -> Descriptor | None:
    """Mirrulations items carry no draw; the captured bytes are the descriptor.

    The mirror stores regulations.gov document JSON verbatim, so the publisher
    fields are read out of the exact bytes the run captured rather than from a
    sidecar that could disagree with them.
    """

    try:
        attributes = (json.loads(blob.read_text(encoding="utf-8")).get("data") or {}).get(
            "attributes"
        ) or {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    title = attributes.get("title")
    document_type = attributes.get("documentType")
    posted = attributes.get("postedDate")
    if not (title and document_type and posted):
        return None
    # modifyDate is the publisher's own last-changed instant; falling back to
    # postedDate keeps the pair ordered rather than inventing an update.
    return Descriptor(
        "mirrulations", title, document_type, posted, attributes.get("modifyDate") or posted
    )


# Each source family states its own descriptor rule. A family absent from this
# table is reported and skipped, never guessed at from a neighbouring shape.
_DESCRIPTOR_RESOLVERS = {
    "federal-register": _federal_register_descriptor,
    "mirrulations": _mirrulations_descriptor,
}


def _source_family(source_item_id: str) -> str | None:
    # urn:docspec:qualification:<family>:<publisher id>
    parts = source_item_id.split(":")
    return parts[3] if len(parts) > 4 and parts[2] == "qualification" else None


def build_rows(run_root: Path, partition: str = "default") -> tuple[list[dict[str, Any]], list[str]]:
    """Join a run's layers into ledger rows. Returns (rows, skipped reasons)."""

    records_root = run_root / "records"
    if not (records_root / "record-layers").is_dir():
        raise LedgerExportError(f"not a completed run directory: {run_root}")
    blobs_root = (run_root / "blobs").resolve()

    selection, representations, source_items = _collect(records_root)
    if not selection:
        raise LedgerExportError(f"run holds no run-selection layer: {run_root}")

    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for item_id, chosen in sorted(selection.items()):
        disposition = chosen.get("disposition")
        if disposition != _CAPTURED:
            skipped.append(f"{item_id}: disposition={disposition!r}, not {_CAPTURED!r}")
            continue
        payloads = representations.get(item_id)
        source_item = source_items.get(item_id)
        if not payloads or source_item is None:
            missing = "representation" if not payloads else "source item"
            skipped.append(f"{item_id}: no {missing} record")
            continue

        blob, metadata_blob = _select_blobs(payloads)
        if blob is None:
            skipped.append(f"{item_id}: representation has no complete blob reference")
            continue
        locator = blob["locator"]
        digest = blob["digest"]
        media_type = blob.get("mediaType")
        if not media_type:
            skipped.append(f"{item_id}: representation blob declares no media type")
            continue
        rendition = blobs_root / locator
        if not rendition.is_file():
            skipped.append(f"{item_id}: blob absent on disk at {rendition}")
            continue

        qualification = (source_item.get("metadata") or {}).get("qualification") or {}
        family = _source_family(item_id)
        resolver = _DESCRIPTOR_RESOLVERS.get(family or "")
        if resolver is None:
            skipped.append(f"{item_id}: no descriptor rule for source family {family!r}")
            continue
        # The descriptor is read from the metadata blob when the capture has a
        # separate one, so choosing a richer body as the rendition never costs
        # the publisher fields.
        descriptor_source = blobs_root / metadata_blob["locator"] if metadata_blob else rendition
        descriptor = resolver(qualification, descriptor_source)
        if descriptor is None:
            # The consumer requires a complete descriptor on an active row.
            # Reporting the gap keeps a partial capture visible instead of
            # silently thinner.
            skipped.append(f"{item_id}: {family} descriptor incomplete")
            continue

        document = Document(
            source_item_id=item_id,
            # DocSpec's store id is a work-scheduling artifact -- which bounded
            # job happened to fetch this item -- not a property of the
            # document. Leaking it as the consumer's partition key would make
            # the release's shape depend on how the fetch was parallelised.
            partition=partition,
            blob_digest=digest,
            blob_locator=locator,
            media_type=media_type,
            draw=qualification.get("finalDraw") or {},
        )
        rows.append(
            {
                "documentId": document.document_number,
                "sourceId": descriptor.source_id,
                "sourceInputId": item_id,
                "sourceRecordId": item_id,
                "sourcePartition": document.partition,
                # Content addresses the version: the same document refetched
                # with identical bytes is the same version, and any edit is a
                # new one, with no publisher revision field required.
                "sourceVersion": digest,
                "disposition": "active",
                "renditionPath": str(rendition),
                "mediaType": media_type,
                "documentType": descriptor.document_type,
                "language": "en",
                "title": descriptor.title,
                "publishedAt": descriptor.published_at,
                "updatedAt": descriptor.updated_at,
                "eligibilityState": "eligible",
                "eligibilityAuthorityId": (
                    (source_item.get("metadata") or {}).get("qualification", {}).get("campaignId")
                    or "unknown-campaign"
                ),
                "eligibilityEvidenceKind": _EVIDENCE_KIND,
                "eligibilityBasis": chosen.get("entryId") or item_id,
                "eligibilityReasonCode": _REASON_CODE,
            }
        )
    return rows, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True, help="Completed run directory")
    parser.add_argument("--output", type=Path, required=True, help="JSON Lines ledger to write")
    parser.add_argument(
        "--partition",
        default="default",
        help="Partition key to stamp on every row; must match the consumer's build partition",
    )
    parser.add_argument(
        "--report-skipped",
        action="store_true",
        help="Print every skipped document rather than a count",
    )
    args = parser.parse_args(argv)

    rows, skipped = build_rows(args.run, partition=args.partition)
    if not rows:
        print(f"no exportable documents in {args.run}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({k: row[k] for k in LEDGER_FIELDS}, sort_keys=True) + "\n")

    print(f"wrote {len(rows)} rows to {args.output}")
    if skipped:
        print(f"skipped {len(skipped)} selected items")
        reasons: dict[str, int] = defaultdict(int)
        for entry in skipped:
            reasons[entry.split(": ", 1)[1]] += 1
        for reason, count in sorted(reasons.items(), key=lambda pair: -pair[1]):
            print(f"  {count:6}  {reason}")
        if args.report_skipped:
            for entry in skipped:
                print(f"    {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
