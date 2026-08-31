#!/usr/bin/env python3
"""Measure one retention floor per (parser, format) on a named, pinned population.

`docs/decisions/0001-document-release-2-0.md`, *Acceptance gates*: "every parser
declares a retention floor measured on a named population before it may run; an
undeclared floor or an unmeasurable source fails closed." This tool is where
that measurement happens, and its committed receipt is the only place the
builder reads a floor from -- so a floor exists because somebody measured it,
not because somebody typed it.

What amendment B5 changed, and why
----------------------------------
The first real mint's calibration was wrong in four ways at once, and this file
is the correction of all four.

**The metric.** Retention was retained representation bytes over RAW captured
bytes. Federal Register XML is 48-60% pretty-print indentation, so that
denominator counted the publisher's formatting as content the parser had failed
to keep, and nine documents that extract completely were refused. Retention is
now measured on both sides after collapsing whitespace runs
(`normalized_byte_size`): the publisher's indentation cancels and a thin parse
still craters the ratio, because the numerator is what survived. Both sides,
not just the denominator -- the HTML extractor lays out verbatim and the XML
extractor lays out normalized, so an un-normalized numerator makes the two
incommensurable, and measured, it produces retention ABOVE 1.0 on 993 real
`distribution-html` documents, which this floor's arithmetic cannot represent.

The RAW ratio is still recorded per document, because normalization hides
exactly one thin-parse mode: content whose meaning is carried BY its whitespace
-- a positional table, an indented hierarchy -- destroyed by a parse that keeps
the characters and drops the layout. A cratered raw ratio under a healthy
normalized one is that signature, and nothing else in this format reports it.

**The population.** The floors were pooled with the corpus they gate, so the
calibration could not refuse anything it had not already admitted. Each floor is
now measured on a population DISJOINT from the gated corpus, and the
disjointness is measured two ways rather than asserted: the renditions are
content-addressed, so their digests are intersected with the pinned corpus's
captured digests, and -- because the same Federal Register document can be
retrieved twice into different bytes -- their source document numbers are
intersected with the pinned corpus's too. A digest check alone would have missed
a redraw of the same document.

**The sample.** `observedMinimum: 0.4777` was a 400-document stride-sample
statistic sealed under a field named for the population minimum, and a release
carried it as one. Both floors are now measured over the FULL named population,
every document, and the receipt has no way to say otherwise: it carries one row
per document and `population.measuredCount` must equal that count.

**The named-and-unused population.** The decision named `distribution-html` and
the first calibration never read it. It is the HTML population now.

The two populations
-------------------
Both from the decision's own preserved body corpus
`_preserved-2026-08-10/body-retrieval-corpus-2026-08-02`:

* **XML** -- `distribution-xml`, 993 Federal Register `RULE`/`PRORULE`
  renditions. Same publisher, same format as the pinned corpus's 6,408 XML
  documents, drawn from a different set of documents.
* **HTML** -- `distribution-html`, the same 993 documents as
  federalregister.gov HTML.

`rule-bodies-pre2000-2026-08-22` is measured and NOT pooled, recorded so the
choice can be re-argued: over its full 39,785 bodies the minimum is 0.0589
against a next-lowest of 0.2481, so pooling it would set an HTML floor of 0.044
-- a floor that gates nothing, chosen by one outlier.

The margin rule
---------------
`RetentionFloor` requires `observed_minimum > value`: no margin under the lowest
legitimate document is a future false refusal. So the floor is **not** a low
quantile of the distribution -- a p1 floor would refuse the bottom percent of the
very population that defined it. It is the observed minimum reduced by a
declared margin: three quarters of the lowest document in the population,
truncated to two significant digits so the number a release carries is one a
reader can hold. The predecessor's floors sit in the same relation to their own
minima (0.75 against 0.9453, 0.85 against 0.9930), which is the precedent the
decision keeps -- its numbers are not adopted, its method is.

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
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from docspec.domain.identity import canonical_json_file_bytes
from docspec.processing.retention_floors import (
    NORMALIZED_VISIBLE_TEXT_FRACTION,
    RetentionFloor,
    decimal_fraction,
    normalized_byte_size,
)
from docspec.processing.visible_text import (
    HtmlVisibleTextExtractor,
    VisibleTextError,
    XmlVisibleTextExtractor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = REPO_ROOT / "fixtures" / "retention-floors" / "calibration.json"

RECEIPT_FORMAT = "docspec-retention-floor-calibration"
RECEIPT_FORMAT_VERSION = "2.0"
RECEIPT_SCHEMA_VERSION = "2.0"

CORPORA_ROOT_VARIABLE = "DOCSPEC_CORPORA_ROOT"
DEFAULT_CORPORA_ROOT = Path.home() / "Work" / "corpora"

PRESERVED_ROOT = "_preserved-2026-08-10/body-retrieval-corpus-2026-08-02"

# Three quarters of the population's observed minimum, as an exact rational.
MARGIN = Fraction(3, 4)
SIGNIFICANT_DIGITS = 2

DOCUMENT_BODY = "document-body"
GATED_CORPUS = "fr-mirrulations-10k-v1 (the corpus this floor gates)"

METRIC: dict[str, str] = {
    "denominator": (
        "the captured rendition's bytes, whitespace-normalized: every maximal run of ASCII "
        "whitespace becomes one space and the ends are stripped"
    ),
    "metricId": NORMALIZED_VISIBLE_TEXT_FRACTION,
    "normalization": (
        "both sides by the same rule, so the publisher's indentation cancels and the two "
        "extractors -- one laying out verbatim, one laying out normalized -- are commensurable"
    ),
    "numerator": "the extracted representation's bytes, whitespace-normalized by the same rule",
}


@dataclass(frozen=True, slots=True)
class Population:
    """One named body corpus, measured whole, and the floor it calibrates."""

    population_id: str
    description: str
    format_key: str
    relative_root: str
    suffix: str
    receipts_root: str


POPULATIONS: tuple[Population, ...] = (
    Population(
        population_id="fr-body-retrieval-2026-08-02:distribution-xml",
        description=(
            "Every Federal Register RULE/PRORULE XML rendition of the 2026-08-02 body-retrieval "
            "draw, one of the two preserved populations Decision 0001 names for the HTML/XML "
            "floors. Same publisher and same format as the pinned corpus's XML half, drawn from a "
            "different set of documents."
        ),
        format_key="application/xml",
        relative_root=f"{PRESERVED_ROOT}/distribution-xml/renditions",
        suffix=".xml",
        receipts_root=f"{PRESERVED_ROOT}/distribution-xml/receipts",
    ),
    Population(
        population_id="fr-body-retrieval-2026-08-02:distribution-html",
        description=(
            "The same draw's federalregister.gov HTML renditions. Decision 0001 named this "
            "population and the first calibration never read it; amendment B5 makes it the HTML "
            "floor's population."
        ),
        format_key="text/html",
        relative_root=f"{PRESERVED_ROOT}/distribution-html/renditions",
        suffix=".html",
        receipts_root=f"{PRESERVED_ROOT}/distribution-html/receipts",
    ),
)

EXTRACTORS = {
    "application/xml": XmlVisibleTextExtractor(),
    "text/html": HtmlVisibleTextExtractor(),
}


def packaged_receipt_schema(version: str = RECEIPT_SCHEMA_VERSION) -> dict[str, Any]:
    """The sealed receipt contract, read from the installed package.

    Resolved through `importlib.resources` rather than from a repository path:
    a consumer verifying a release's declared floors has the package and not
    this checkout.
    """

    root = resources.files("docspec") / "schemas" / "retention_floor_calibration" / version
    path = Path(str(root)) / "retention-floor-calibration.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def corpora_root() -> Path | None:
    override = os.environ.get(CORPORA_ROOT_VARIABLE)
    candidate = Path(override) if override else DEFAULT_CORPORA_ROOT
    return candidate if candidate.is_dir() else None


def _source_documents(population: Population, corpora: Path) -> dict[str, str]:
    """Every rendition digest in this population, mapped to its source document.

    The draw's own receipts carry the mapping. It exists for the disjointness
    check: two different retrievals of one Federal Register document have two
    digests and one document number, and only the second catches them.
    """

    mapping: dict[str, str] = {}
    root = corpora / population.receipts_root
    if not root.is_dir():
        raise SystemExit(f"calibration population {population.population_id} has no receipts: {root}")
    for path in sorted(root.glob("*.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        for source in receipt.get("sources", ()):
            document = source.get("document_number") or source.get("case_id")
            if isinstance(source.get("source_sha256"), str) and document:
                mapping[source["source_sha256"]] = str(document)
    return mapping


def _gated_corpus_identities(
    captures: Mapping[str, Mapping[str, Any]] | None,
    items: Sequence[Mapping[str, Any]] | None,
) -> tuple[set[str], set[str]]:
    """The gated corpus's captured digests and its source document identifiers."""

    digests = {
        capture.digest.removeprefix("sha256:")
        for found in (captures or {}).values()
        for capture in found.values()
    }
    documents: set[str] = set()
    for item in items or ():
        qualification = item["metadata"]["qualification"]
        if qualification.get("source") == "federal-register":
            number = (qualification.get("finalDraw") or {}).get("document_number")
            if number:
                documents.add(str(number))
        elif qualification.get("documentId"):
            documents.add(str(qualification["documentId"]))
    return digests, documents


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
    by_normalized = sorted(
        rows,
        key=lambda row: Fraction(
            row["normalizedRepresentationByteSize"], row["normalizedRenditionByteSize"]
        ),
    )
    by_raw = sorted(
        rows,
        key=lambda row: Fraction(row["representationByteSize"], row["renditionByteSize"]),
    )

    def quantile(fraction: float) -> str:
        return by_normalized[min(len(by_normalized) - 1, int(fraction * len(by_normalized)))][
            "retention"
        ]

    return {
        "count": len(rows),
        "maximum": by_normalized[-1]["retention"],
        "median": quantile(0.5),
        "minimum": by_normalized[0]["retention"],
        "p01": quantile(0.01),
        "p05": quantile(0.05),
        "p95": quantile(0.95),
        "rawMedian": by_raw[len(by_raw) // 2]["rawRetention"],
        "rawMinimum": by_raw[0]["rawRetention"],
    }


def _measure_documents(
    population: Population, corpora: Path, documents: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Extract every document in the population and measure both ratios.

    An extraction failure stops the calibration rather than being skipped: a
    floor measured over the documents that happened to parse is a floor
    calibrated on its own success.
    """

    extractor = EXTRACTORS[population.format_key]
    root = corpora / population.relative_root
    if not root.is_dir():
        raise SystemExit(f"calibration population {population.population_id} is absent: {root}")
    paths = sorted(
        (path for path in root.iterdir() if path.is_file() and path.suffix == population.suffix),
        key=lambda path: path.stem,
    )
    if not paths:
        raise SystemExit(f"calibration population {population.population_id} holds no documents")
    rows: list[dict[str, Any]] = []
    for path in paths:
        source = path.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        try:
            visible = extractor.extract(source)
        except VisibleTextError as error:
            raise SystemExit(
                f"calibration refuses to declare a floor: {path.stem} in "
                f"{population.population_id} could not be extracted ({error.reason_code}: {error})"
            ) from error
        normalized_source = normalized_byte_size(source)
        normalized_retained = normalized_byte_size(visible.content)
        rows.append(
            {
                "documentId": documents.get(digest, path.stem),
                "normalizedRenditionByteSize": normalized_source,
                "normalizedRepresentationByteSize": normalized_retained,
                "rawRetention": decimal_fraction(len(visible.content), len(source)),
                "renditionByteSize": len(source),
                "renditionSha256": digest,
                "representationByteSize": len(visible.content),
                "retention": decimal_fraction(normalized_retained, normalized_source),
            }
        )
    rows.sort(key=lambda row: row["documentId"])
    return rows


def measure(
    population: Population,
    corpora: Path,
    gated_digests: set[str],
    gated_documents: set[str],
) -> dict[str, Any]:
    """Measure one population whole and derive the floor it declares."""

    documents_by_digest = _source_documents(population, corpora)
    rows = _measure_documents(population, corpora, documents_by_digest)
    shared_digests = sorted({row["renditionSha256"] for row in rows} & gated_digests)
    shared_documents = sorted({row["documentId"] for row in rows} & gated_documents)
    if shared_digests or shared_documents:
        # A floor calibrated on the population it gates cannot refuse anything
        # in it. Overlap is dropped rather than tolerated, and the counts stay
        # in the receipt so the drop is visible.
        rows = [
            row
            for row in rows
            if row["renditionSha256"] not in gated_digests
            and row["documentId"] not in gated_documents
        ]
        if not rows:
            raise SystemExit(
                f"calibration population {population.population_id} is entirely inside the "
                "corpus it would gate"
            )
    lowest = min(
        rows,
        key=lambda row: Fraction(
            row["normalizedRepresentationByteSize"], row["normalizedRenditionByteSize"]
        ),
    )
    exact_minimum = Fraction(
        lowest["normalizedRepresentationByteSize"], lowest["normalizedRenditionByteSize"]
    )
    floor = RetentionFloor(
        value=_two_significant_digits_below(exact_minimum * MARGIN),
        unit=NORMALIZED_VISIBLE_TEXT_FRACTION,
        observed_minimum=lowest["retention"],
        population=population.population_id,
    )
    extractor = EXTRACTORS[population.format_key]
    inventory = [
        {
            "byteSize": row["renditionByteSize"],
            "documentId": row["documentId"],
            "sha256": row["renditionSha256"],
        }
        for row in rows
    ]
    return {
        "distribution": _distribution(rows),
        "documents": rows,
        "extractorConfiguration": extractor.configuration,
        "extractorConfigurationDigest": extractor.configuration_digest,
        "extractorId": extractor.extractor_id,
        "formatKey": population.format_key,
        "lowestDocument": lowest,
        "marginRule": (
            f"floor = {MARGIN.numerator}/{MARGIN.denominator} of the population's observed "
            f"minimum, truncated to {SIGNIFICANT_DIGITS} significant digits"
        ),
        "observedMinimum": lowest["retention"],
        "population": {
            "coverage": "full-population",
            "description": population.description,
            "disjointness": {
                "against": GATED_CORPUS,
                "sharedRenditionDigests": len(shared_digests),
                "sharedSourceDocuments": len(shared_documents),
            },
            "documentCount": len(rows),
            "inventoryDigest": hashlib.sha256(
                json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "measuredCount": len(rows),
            "populationId": population.population_id,
            "root": population.relative_root,
        },
        "retentionFloor": floor.to_dict(),
        "textKind": DOCUMENT_BODY,
    }


def calibrate(
    corpora: Path,
    captures: Mapping[str, Mapping[str, Any]] | None = None,
    items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    gated_digests, gated_documents = _gated_corpus_identities(captures, items)
    measurements = [
        measure(population, corpora, gated_digests, gated_documents)
        for population in POPULATIONS
    ]
    measurements.sort(key=lambda row: (row["textKind"], row["formatKey"]))
    receipt = {
        "format": RECEIPT_FORMAT,
        "formatVersion": RECEIPT_FORMAT_VERSION,
        "measurements": measurements,
        "metric": dict(METRIC),
    }
    validate_receipt(receipt)
    return receipt


def validate_receipt(receipt: Mapping[str, Any]) -> None:
    """Refuse a receipt the sealed contract does not admit.

    The schema is what makes the honesty structural rather than conventional:
    it has no field named `sample`, `population.coverage` is a constant, and the
    two consistency rules below close the gap a schema cannot express -- that
    the declared count IS the measured one, and that `observedMinimum` IS the
    minimum of the distribution beside it.
    """

    jsonschema.Draft202012Validator(packaged_receipt_schema()).validate(receipt)
    for measurement in receipt["measurements"]:
        population = measurement["population"]
        label = population["populationId"]
        if population["measuredCount"] != population["documentCount"]:
            raise ValueError(f"{label} measured {population['measuredCount']} of its documents")
        if len(measurement["documents"]) != population["documentCount"]:
            raise ValueError(f"{label} declares a count its document rows do not support")
        if measurement["distribution"]["count"] != population["documentCount"]:
            raise ValueError(f"{label} distributed a different count than it measured")
        if measurement["observedMinimum"] != measurement["distribution"]["minimum"]:
            raise ValueError(f"{label} observedMinimum is not the minimum it measured")
        if measurement["observedMinimum"] != measurement["retentionFloor"]["observedMinimum"]:
            raise ValueError(f"{label} declares two different observed minima")


def load_receipt(receipt_path: Path = RECEIPT_PATH) -> dict[str, Any]:
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if (
        receipt.get("format") != RECEIPT_FORMAT
        or receipt.get("formatVersion") != RECEIPT_FORMAT_VERSION
    ):
        raise ValueError("retention floor calibration receipt has an unknown format")
    validate_receipt(receipt)
    return receipt


def load_floors(receipt_path: Path = RECEIPT_PATH) -> dict[tuple[str, str], RetentionFloor]:
    """The declared floors, read back through their own invariants.

    The builder reads floors here and nowhere else. A receipt whose floor lost
    its margin, or whose value left the open unit interval, or whose observed
    minimum is not the minimum it measured, fails here rather than gating a mint.
    """

    floors: dict[tuple[str, str], RetentionFloor] = {}
    for measurement in load_receipt(receipt_path)["measurements"]:
        key = (measurement["textKind"], measurement["formatKey"])
        if key in floors:
            raise ValueError(f"retention floor calibration declares {key} twice")
        floors[key] = RetentionFloor.from_dict(measurement["retentionFloor"])
    return floors


def load_policies(receipt_path: Path = RECEIPT_PATH) -> dict[tuple[str, str], dict[str, Any]]:
    """The extractor identity each floor was measured through, keyed with it."""

    return {
        (measurement["textKind"], measurement["formatKey"]): {
            "extractorDigest": measurement["extractorConfigurationDigest"],
            "extractorId": measurement["extractorId"],
        }
        for measurement in load_receipt(receipt_path)["measurements"]
    }


def main(argv: list[str] | None = None) -> int:
    from tools.fr_mirrulations_pin import catalog_items, load_pin, preserved_captures

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace the committed calibration receipt")
    parser.add_argument("--corpora", type=Path, default=None, help="corpora root override")
    arguments = parser.parse_args(argv)

    corpora = arguments.corpora or corpora_root()
    if corpora is None:
        print(f"the calibration populations are absent; set {CORPORA_ROOT_VARIABLE} to relocate them")
        return 1
    pinned = load_pin()
    receipt = calibrate(corpora, preserved_captures(pinned), list(catalog_items(pinned)))
    for measurement in receipt["measurements"]:
        floor = measurement["retentionFloor"]
        population = measurement["population"]
        overlap = population["disjointness"]
        print(
            f"{measurement['textKind']:14s} {measurement['formatKey']:18s} floor {floor['value']:6s} "
            f"under observed minimum {floor['observedMinimum']:8s} "
            f"(median {measurement['distribution']['median']}, "
            f"raw median {measurement['distribution']['rawMedian']}, "
            f"n={population['documentCount']} whole) on {floor['population']}"
        )
        print(
            f"{'':14s} disjointness: {overlap['sharedRenditionDigests']} shared digests, "
            f"{overlap['sharedSourceDocuments']} shared documents"
        )
    if arguments.write:
        RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT_PATH.write_bytes(canonical_json_file_bytes(receipt))
        print(f"wrote {RECEIPT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
