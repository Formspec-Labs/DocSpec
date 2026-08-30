#!/usr/bin/env python3
"""Measure one retention floor per (parser, format) on a named, pinned population.

`docs/decisions/0001-document-release-2-0.md`, *Acceptance gates*: "every parser
declares a retention floor measured on a named population before it may run; an
undeclared floor or an unmeasurable source fails closed." This tool is where
that measurement happens, and its committed receipt is the only place the
builder reads a floor from -- so a floor exists because somebody measured it,
not because somebody typed it.

The populations are the decision's own
--------------------------------------
The decision names two preserved body corpora and this tool uses both, choosing
between them by document family rather than by media type, because a floor
measured on the wrong family is a floor measured on the wrong denominator:

* **XML** -- `_preserved-2026-08-10/body-retrieval-corpus-2026-08-02/distribution-xml`,
  995 Federal Register `RULE`/`PRORULE` renditions. The pinned corpus's 6,408
  XML documents are the same publisher's same format.
* **HTML** -- `_salvage-2026-08-28/spicysearch-output/rule-bodies-pre2000-2026-08-22`,
  39,786 pre-2000 rule bodies. The decision calls this "the hardest case for a
  visible-text floor", and it is also the *right* case: every one of them is the
  same `<html><head><title></head><body><pre>` GPO carrier the pinned corpus's
  Mirrulations documents use. The other named HTML population --
  `distribution-html` -- is federalregister.gov's modern `div`-and-class markup,
  a different family whose retention profile would gate `<pre>` bodies against a
  denominator they were never drawn from.

Each floor is measured on **two strata pooled**: the preserved corpus above,
and a deterministic sample of the pinned corpus this mint reads. Both are
needed, and the reason is the denominator. The preserved XML draw holds no
document under 37 KB, and a short Federal Register notice spends a far larger
share of its bytes on preamble markup than a long rule does, so a floor measured
on the preserved draw alone sits above documents the mint corpus legitimately
contains -- the false refusal `RetentionFloor` exists to prevent. Pooling puts
both size ranges under the same minimum. The preserved stratum keeps the floor
honest about documents this mint never saw; the pinned stratum keeps it honest
about the ones it will. Each stratum's own distribution is recorded separately,
so the pooling can be re-argued from the receipt.

A floor calibrated only on the population it gates can refuse nothing in it, and
a floor calibrated only away from it refuses the wrong things. The sample is a
sample, though, not the population: a document outside the drawn sample may
still fall below the floor, and refusing it is the mechanism working rather than
failing.

The margin rule
---------------
`RetentionFloor` requires `observed_minimum > value`: no margin under the lowest
legitimate document is a future false refusal. So the floor is **not** a low
quantile of the distribution -- a p1 floor would refuse the bottom percent of the
very population that defined it. It is the observed minimum reduced by a
declared margin: three quarters of the lowest document the sample contained,
rounded down to two significant digits so the number a release carries is one a
reader can hold. The predecessor's floors sit in the same relation to their own
minima (0.75 against 0.9453, 0.85 against 0.9930, 0.005 against 0.015584), which
is the precedent the decision keeps -- its numbers are not adopted, its method
is.

The distribution is recorded beside the floor so the margin can be re-argued
from the receipt without re-running the measurement.

Usage:
  uv run python -m tools.calibrate_retention_floors
  uv run python -m tools.calibrate_retention_floors --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from docspec.domain.identity import canonical_json_file_bytes
from docspec.processing.retention_floors import (
    VISIBLE_TEXT_FRACTION,
    RetentionFloor,
    decimal_fraction,
)
from docspec.processing.visible_text import (
    HtmlVisibleTextExtractor,
    VisibleTextError,
    XmlVisibleTextExtractor,
)
from tools.fr_mirrulations_pin import load_pin, preserved_captures

REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = REPO_ROOT / "fixtures" / "retention-floors" / "calibration.json"

RECEIPT_FORMAT = "docspec-retention-floor-calibration"
RECEIPT_FORMAT_VERSION = "1.0"

CORPORA_ROOT_VARIABLE = "DOCSPEC_CORPORA_ROOT"
DEFAULT_CORPORA_ROOT = Path.home() / "Work" / "corpora"

# How many documents each floor is measured on, and how they are chosen: sort
# every candidate identifier, then stride through the sorted list so the sample
# spans the population instead of the first slice of it. Both are recorded in
# the receipt, so the same sample is redrawable from the same corpus.
SAMPLE_SIZE = 200
# Three quarters of the lowest document measured, as an exact rational.
MARGIN = Fraction(3, 4)
SIGNIFICANT_DIGITS = 2

DOCUMENT_BODY = "document-body"


@dataclass(frozen=True, slots=True)
class Stratum:
    """One named body corpus contributing to a floor, and how it is sampled."""

    stratum_id: str
    description: str
    relative_root: str
    suffix: str = ""
    candidate_id: str = ""


@dataclass(frozen=True, slots=True)
class Population:
    """One (text kind, format) floor and the strata pooled to measure it."""

    population_id: str
    format_key: str
    strata: tuple[Stratum, ...]


PRESERVED_XML = Stratum(
    stratum_id="fr-body-retrieval-2026-08-02:distribution-xml",
    description=(
        "995 Federal Register RULE/PRORULE XML renditions from the 2026-08-02 body-retrieval draw, "
        "the preserved population Decision 0001 names for the HTML/XML floors"
    ),
    relative_root="_preserved-2026-08-10/body-retrieval-corpus-2026-08-02/distribution-xml/renditions",
    suffix=".xml",
)
PRESERVED_HTML = Stratum(
    stratum_id="fr-rule-bodies-pre2000-2026-08-22",
    description=(
        "39,786 pre-2000 Federal Register rule bodies in the GPO <pre> carrier, the population "
        "Decision 0001 names as the hardest case for a visible-text floor"
    ),
    relative_root="_salvage-2026-08-28/spicysearch-output/rule-bodies-pre2000-2026-08-22/bodies",
    suffix=".htm",
)
PINNED_XML = Stratum(
    stratum_id="fr-mirrulations-10k-v1:federal-register-xml",
    description="the pinned corpus's own 6,408 preserved Federal Register XML renditions",
    relative_root="",
    candidate_id="federal-register-xml",
)
PINNED_HTML = Stratum(
    stratum_id="fr-mirrulations-10k-v1:rendition-html",
    description="the pinned corpus's own preserved Mirrulations HTML renditions",
    relative_root="",
    candidate_id="rendition-html",
)

POPULATIONS: tuple[Population, ...] = (
    Population(
        population_id="fr-body-retrieval-distribution-xml + fr-mirrulations-10k-v1-xml",
        format_key="application/xml",
        strata=(PRESERVED_XML, PINNED_XML),
    ),
    Population(
        population_id="fr-rule-bodies-pre2000 + fr-mirrulations-10k-v1-html",
        format_key="text/html",
        strata=(PRESERVED_HTML, PINNED_HTML),
    ),
)

EXTRACTORS = {
    "application/xml": XmlVisibleTextExtractor(),
    "text/html": HtmlVisibleTextExtractor(),
}


def corpora_root() -> Path | None:
    override = os.environ.get(CORPORA_ROOT_VARIABLE)
    candidate = Path(override) if override else DEFAULT_CORPORA_ROOT
    return candidate if candidate.is_dir() else None


def _stride(candidates: Sequence[Any], stratum_id: str, size: int) -> list[Any]:
    """Take a sample that spans a sorted population rather than its first slice."""

    if len(candidates) < size:
        raise SystemExit(
            f"calibration stratum {stratum_id} holds {len(candidates)} documents, "
            f"fewer than the declared sample of {size}"
        )
    stride = len(candidates) // size
    return [candidates[index * stride] for index in range(size)]


def draw(
    stratum: Stratum,
    corpora: Path,
    captures: Mapping[str, Mapping[str, Any]] | None,
    *,
    size: int = SAMPLE_SIZE,
) -> list[tuple[str, bytes]]:
    """One stratum's deterministic sample, as identified documents and their bytes."""

    if stratum.candidate_id:
        if captures is None:
            raise SystemExit(f"calibration stratum {stratum.stratum_id} needs the pinned corpus")
        matching = sorted(
            (item_id, found[stratum.candidate_id])
            for item_id, found in captures.items()
            if stratum.candidate_id in found
        )
        return [
            (item_id, capture.read())
            for item_id, capture in _stride(matching, stratum.stratum_id, size)
        ]
    root = corpora / stratum.relative_root
    if not root.is_dir():
        raise SystemExit(f"calibration stratum {stratum.stratum_id} is absent: {root}")
    paths = sorted(
        (path for path in root.iterdir() if path.is_file() and path.suffix == stratum.suffix),
        key=lambda path: path.stem,
    )
    return [
        (path.stem, path.read_bytes())
        for path in _stride(paths, stratum.stratum_id, size)
    ]


def _two_significant_digits_below(value: Fraction) -> str:
    """The largest two-significant-digit decimal at or below one fraction below 1."""

    if value <= 0 or value >= 1:
        raise SystemExit(f"a retention floor target of {float(value)} is not a fraction below 1")
    places = 1
    while value < Fraction(1, 10**places):
        places += 1
    scale = 10 ** (places + SIGNIFICANT_DIGITS - 1)
    scaled = int(value * scale)
    if scaled <= 0:
        raise SystemExit(f"a retention floor target of {float(value)} truncates to zero")
    text = f"{scaled:0{places + SIGNIFICANT_DIGITS}d}"
    digits = places + SIGNIFICANT_DIGITS - 1
    return ("0." + text[-digits:]).rstrip("0")


def _distribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: Fraction(row["representationByteSize"], row["renditionByteSize"]))

    def quantile(fraction: float) -> str:
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]["retention"]

    return {
        "count": len(ordered),
        "maximum": ordered[-1]["retention"],
        "median": quantile(0.5),
        "minimum": ordered[0]["retention"],
        "p01": quantile(0.01),
        "p05": quantile(0.05),
        "p95": quantile(0.95),
    }


def _extract_rows(
    stratum: Stratum, format_key: str, samples: Sequence[tuple[str, bytes]]
) -> list[dict[str, Any]]:
    extractor = EXTRACTORS[format_key]
    rows: list[dict[str, Any]] = []
    for document_id, source in samples:
        try:
            visible = extractor.extract(source)
        except VisibleTextError as error:
            raise SystemExit(
                f"calibration refuses to declare a floor: {document_id} in "
                f"{stratum.stratum_id} could not be extracted ({error.reason_code}: {error})"
            ) from error
        rows.append(
            {
                "documentId": document_id,
                "renditionByteSize": len(source),
                "renditionSha256": hashlib.sha256(source).hexdigest(),
                "representationByteSize": len(visible.content),
                "retention": decimal_fraction(len(visible.content), len(source)),
                "stratum": stratum.stratum_id,
            }
        )
    rows.sort(key=lambda row: row["documentId"])
    return rows


def measure(
    population: Population,
    corpora: Path,
    captures: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Extract every sampled document, pool the strata, and derive the floor."""

    strata: list[dict[str, Any]] = []
    pooled: list[dict[str, Any]] = []
    for stratum in population.strata:
        rows = _extract_rows(stratum, population.format_key, draw(stratum, corpora, captures))
        pooled.extend(rows)
        strata.append(
            {
                "description": stratum.description,
                "distribution": _distribution(rows),
                "root": stratum.relative_root or f"pinned-corpus:{stratum.candidate_id}",
                "sample": rows,
                "sampleDigest": hashlib.sha256(
                    json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "stratumId": stratum.stratum_id,
            }
        )
    lowest = min(pooled, key=lambda row: Fraction(row["representationByteSize"], row["renditionByteSize"]))
    exact_minimum = Fraction(lowest["representationByteSize"], lowest["renditionByteSize"])
    floor = RetentionFloor(
        value=_two_significant_digits_below(exact_minimum * MARGIN),
        unit=VISIBLE_TEXT_FRACTION,
        observed_minimum=lowest["retention"],
        population=population.population_id,
    )
    extractor = EXTRACTORS[population.format_key]
    return {
        "distribution": _distribution(pooled),
        "extractorConfiguration": extractor.configuration,
        "extractorConfigurationDigest": extractor.configuration_digest,
        "extractorId": extractor.extractor_id,
        "formatKey": population.format_key,
        "lowestDocument": lowest,
        "marginRule": (
            f"floor = {MARGIN.numerator}/{MARGIN.denominator} of the pooled observed minimum, "
            f"truncated to {SIGNIFICANT_DIGITS} significant digits"
        ),
        "population": population.population_id,
        "retentionFloor": floor.to_dict(),
        "samplePolicy": {
            "orderedBy": "documentId",
            "sizePerStratum": SAMPLE_SIZE,
            "strategy": "sorted-stride",
        },
        "strata": strata,
        "textKind": DOCUMENT_BODY,
    }


def calibrate(corpora: Path, captures: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    measurements = [measure(population, corpora, captures) for population in POPULATIONS]
    measurements.sort(key=lambda row: (row["textKind"], row["formatKey"]))
    return {
        "format": RECEIPT_FORMAT,
        "formatVersion": RECEIPT_FORMAT_VERSION,
        "measurements": measurements,
    }


def load_floors(receipt_path: Path = RECEIPT_PATH) -> dict[tuple[str, str], RetentionFloor]:
    """The declared floors, read back through their own invariants.

    The builder reads floors here and nowhere else. A receipt whose floor lost
    its margin, or whose value left the open unit interval, fails in
    `RetentionFloor` rather than gating a mint.
    """

    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if receipt.get("format") != RECEIPT_FORMAT or receipt.get("formatVersion") != RECEIPT_FORMAT_VERSION:
        raise ValueError("retention floor calibration receipt has an unknown format")
    floors: dict[tuple[str, str], RetentionFloor] = {}
    for measurement in receipt["measurements"]:
        key = (measurement["textKind"], measurement["formatKey"])
        if key in floors:
            raise ValueError(f"retention floor calibration declares {key} twice")
        floors[key] = RetentionFloor.from_dict(measurement["retentionFloor"])
    return floors


def load_policies(receipt_path: Path = RECEIPT_PATH) -> dict[tuple[str, str], dict[str, Any]]:
    """The extractor identity each floor was measured through, keyed with it."""

    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    return {
        (measurement["textKind"], measurement["formatKey"]): {
            "extractorDigest": measurement["extractorConfigurationDigest"],
            "extractorId": measurement["extractorId"],
        }
        for measurement in receipt["measurements"]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace the committed calibration receipt")
    parser.add_argument("--corpora", type=Path, default=None, help="corpora root override")
    arguments = parser.parse_args(argv)

    corpora = arguments.corpora or corpora_root()
    if corpora is None:
        print(f"the calibration populations are absent; set {CORPORA_ROOT_VARIABLE} to relocate them")
        return 1
    pinned = load_pin()
    receipt = calibrate(corpora, preserved_captures(pinned))
    for measurement in receipt["measurements"]:
        floor = measurement["retentionFloor"]
        print(
            f"{measurement['textKind']:14s} {measurement['formatKey']:18s} floor {floor['value']:6s} "
            f"under observed minimum {floor['observedMinimum']:8s} "
            f"(median {measurement['distribution']['median']}, n={measurement['distribution']['count']}) "
            f"on {floor['population']}"
        )
    if arguments.write:
        RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT_PATH.write_bytes(canonical_json_file_bytes(receipt))
        print(f"wrote {RECEIPT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
