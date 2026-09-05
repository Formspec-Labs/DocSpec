"""Measure per-source description coverage, so an evaluation reads against a denominator.

A retrieval score over a field is meaningless without knowing how often the field
is there. The Federal Register topic census made the point sharply: a gate floor
was set at 0.3 from a 93-subject sample when the true ratio is 0.114, and it would
have failed a release on a coverage fact rather than a retrieval defect.

**The two sources name the field differently, and neither normalizes it.**
Federal Register carries ``abstract`` at the root of its source record;
regulations.gov carries ``docAbstract`` under ``data.attributes``. Neither
appears in ``normalizedMetadata``, which holds title, agencies, documentType,
dates, docket ids, RINs, language and sourceUrl and no description at all. So a
consumer reading normalized metadata sees no description on either source, and
one reading source-native facts must know two different field names. Both facts
are reported rather than papered over.

**Empty is counted three ways** -- absent key, null, and present-but-blank --
because they are different upstream conditions and collapsing them would hide
which one a fix would have to address.
"""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

SOURCES = {
    "federal-register-documents": ("abstract", ()),
    "regulations-gov-documents": ("docAbstract", ("data", "attributes")),
}


def _describe(fact: dict[str, Any], scope: str) -> tuple[str | None, str, dict[str, Any]]:
    field, path = SOURCES[scope]
    node = fact["fields"]
    for step in path:
        node = node.get(step) or {}
    if field not in node:
        return None, "absent", node
    value = node[field]
    if value is None:
        return None, "null", node
    if not str(value).strip():
        return None, "blank", node
    return str(value), "present", node


def _scan(args: tuple[str, str]) -> dict[str, Any]:
    path, scope = args
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    counts: Counter[str] = Counter()
    lengths: list[int] = []
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        fact = next(
            (f for f in row.get("sourceNativeFacts") or () if f.get("scopeId") == scope),
            None,
        )
        if fact is None:
            continue
        counts["rows"] += 1
        value, state, node = _describe(fact, scope)
        counts[state] += 1
        if value is not None:
            lengths.append(len(value))
        document_type = node.get("documentType") or node.get("type") or "(none)"
        published = str(node.get("publication_date") or node.get("postedDate") or "")[:4] or "(none)"
        by_type[document_type][state] += 1
        by_year[published][state] += 1
    return {
        "counts": dict(counts),
        "lengths": lengths,
        "byType": {k: dict(v) for k, v in by_type.items()},
        "byYear": {k: dict(v) for k, v in by_year.items()},
    }


def measure(catalog_root: Path, blob_store: Path, scope: str, workers: int) -> dict[str, Any]:
    manifest = json.loads((catalog_root / "manifests" / "catalog.json").read_text())
    members = sorted(
        (m for m in manifest["members"] if m["role"] == "source-items"),
        key=lambda m: m["blobRef"],
    )
    jobs = [(str(blob_store / m["blobRef"].split(":", 1)[1]), scope) for m in members]
    counts: Counter[str] = Counter()
    lengths: list[int] = []
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(_scan, jobs):
            counts.update(part["counts"])
            lengths.extend(part["lengths"])
            for k, v in part["byType"].items():
                by_type[k].update(v)
            for k, v in part["byYear"].items():
                by_year[k].update(v)
    lengths.sort()

    def pct(q: float) -> int:
        return lengths[min(int(q * len(lengths)), len(lengths) - 1)] if lengths else 0

    rows = counts["rows"]
    return {
        "scope": scope,
        "field": SOURCES[scope][0],
        "fieldPath": "/".join((*SOURCES[scope][1], SOURCES[scope][0])),
        "inNormalizedMetadata": False,
        "rows": rows,
        "present": counts["present"],
        "coverage": round(counts["present"] / rows, 4) if rows else 0.0,
        "emptyBreakdown": {
            "null": counts["null"],
            "absent": counts["absent"],
            "blankString": counts["blank"],
        },
        "lengthDistribution": {
            "min": lengths[0] if lengths else 0,
            "p05": pct(0.05),
            "p25": pct(0.25),
            "median": pct(0.5),
            "mean": round(statistics.fmean(lengths), 1) if lengths else 0,
            "p75": pct(0.75),
            "p95": pct(0.95),
            "max": lengths[-1] if lengths else 0,
        },
        "byDocumentType": {
            k: {
                "rows": sum(v.values()),
                "present": v.get("present", 0),
                "coverage": round(v.get("present", 0) / sum(v.values()), 4),
            }
            for k, v in sorted(by_type.items(), key=lambda kv: -sum(kv[1].values()))
        },
        "byYear": {
            k: {
                "rows": sum(v.values()),
                "present": v.get("present", 0),
                "coverage": round(v.get("present", 0) / sum(v.values()), 4),
            }
            for k, v in sorted(by_year.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", action="append", nargs=3, metavar=("ROOT", "BLOBS", "SCOPE"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args(argv)

    measured = [
        measure(Path(root), Path(blobs), scope, args.workers) for root, blobs, scope in args.catalog
    ]
    for m in measured:
        print(f"\n=== {m['scope']} · {m['fieldPath']} ===")
        print(f"  rows {m['rows']:,}   present {m['present']:,}   COVERAGE {m['coverage']:.4f}")
        print(f"  empty: null {m['emptyBreakdown']['null']:,}  absent {m['emptyBreakdown']['absent']:,}  blank {m['emptyBreakdown']['blankString']:,}")
        d = m["lengthDistribution"]
        print(f"  length p05 {d['p05']}  median {d['median']}  mean {d['mean']}  p95 {d['p95']}  max {d['max']}")
        print("  worst-covered document types with >=1000 rows:")
        worst = sorted(
            ((k, v) for k, v in m["byDocumentType"].items() if v["rows"] >= 1000),
            key=lambda kv: kv[1]["coverage"],
        )[:5]
        for k, v in worst:
            print(f"    {k[:38]:38s} {v['coverage']:.3f}  ({v['rows']:,} rows)")

    payload = {
        "receipt": "per-source-description-coverage",
        "purpose": (
            "The denominator an evaluation must read the description axis against. "
            "A floor set from a small sample rather than a census fails releases on "
            "coverage facts."
        ),
        "normalizedMetadataNote": (
            "Neither source's description reaches normalizedMetadata, which carries "
            "title, agencies, documentType, dates, docketIds, RINs, language and "
            "sourceUrl. A consumer reading normalized metadata sees no description at "
            "all; one reading source-native facts must know two field names."
        ),
        "sources": measured,
    }
    args.out.write_bytes(json.dumps(payload, indent=1, sort_keys=True).encode() + b"\n")
    print(f"\nreceipt {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
