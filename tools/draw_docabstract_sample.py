"""Draw 30 real `docAbstract` values for the v13 display-rule check.

SpicySearch is moving text policy 2.1 to 2.2 to read regulations.gov's
`data/attributes/docAbstract` as the document-abstract role. The display rule is
checked the way the inline-text scope was — three readers over 30 real values,
because that method found a display rule the first time.

**Values are reported exactly as stored, never cleaned.** A field that carries
HTML, a placeholder, or a copy of the title is precisely what the display rule
has to handle, so anomalies are *flagged beside* the text rather than repaired
out of it. A sample that quietly normalises its values would prove the display
rule unnecessary by removing the evidence for it.

**The strata ask three different questions.** Notice and Other are the
worst-covered types (0.088 and 0.241), where a populated value is unusual enough
to be worth looking at. Rule and Proposed Rule are the well-covered types, which
say what a normal value looks like. And 2016-or-later is the recency decline —
coverage falls from 0.796 in 1997 to 0.073 in 2018 — so a value that survives
there is drawn from the thinnest part of the corpus.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

SCOPE = "regulations-gov-documents"
PLACEHOLDERS = {"n/a", "na", "none", "null", "-", "--", ".", "tbd", "not applicable", "no abstract"}
HTML = re.compile(r"<[a-zA-Z/][^>]{0,200}>")
ENTITY = re.compile(r"&(?:#\d+|[a-zA-Z]+);")


def _anomalies(text: str, title: str | None) -> list[str]:
    """Name what is odd about a value without altering it."""
    found: list[str] = []
    stripped = text.strip()
    if HTML.search(text):
        found.append("html-tags")
    if ENTITY.search(text):
        found.append("html-entities")
    if stripped.lower() in PLACEHOLDERS:
        found.append("placeholder")
    if title and stripped and stripped == title.strip():
        found.append("identical-to-title")
    elif title and stripped and stripped.lower() in title.strip().lower():
        found.append("contained-in-title")
    if text != stripped:
        found.append("leading-or-trailing-whitespace")
    if "\r" in text:
        found.append("carriage-return")
    if "\n" in stripped:
        found.append("embedded-newline")
    if stripped and stripped == stripped.upper() and any(c.isalpha() for c in stripped):
        found.append("all-caps")
    if "�" in text:
        found.append("replacement-character")
    if len(stripped) < 20:
        found.append("very-short")
    return found


def _scan(args: tuple[str, str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    path, salt = args
    kept: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        fact = next((f for f in row.get("sourceNativeFacts") or () if f.get("scopeId") == SCOPE), None)
        if fact is None:
            continue
        attributes = (fact["fields"].get("data") or {}).get("attributes") or {}
        value = attributes.get("docAbstract")
        counts["rows"] += 1
        if not value or not str(value).strip():
            continue
        counts["populated"] += 1
        document_type = attributes.get("documentType") or "(none)"
        year = str(attributes.get("postedDate") or "")[:4] or "(none)"
        kept.append(
            {
                "documentId": row["documentId"],
                "documentType": document_type,
                "year": year,
                "docAbstract": str(value),
                "length": len(str(value)),
                "anomalies": _anomalies(str(value), attributes.get("title")),
                "rank": hashlib.sha256(f"{salt}\x00{row['documentId']}".encode()).hexdigest(),
            }
        )
    return kept, dict(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--blob-store", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--salt", default="docspec-docabstract-display-2026-09-05")
    parser.add_argument("--per-stratum", type=int, default=10)
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args(argv)

    manifest = json.loads((args.catalog_root / "manifests" / "catalog.json").read_text())
    members = sorted(
        (m for m in manifest["members"] if m["role"] == "source-items"),
        key=lambda m: m["blobRef"],
    )
    jobs = [(str(args.blob_store / m["blobRef"].split(":", 1)[1]), args.salt) for m in members]
    frame: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for kept, member_counts in pool.map(_scan, jobs):
            frame.extend(kept)
            counts.update(member_counts)

    strata = {
        "populated-notice-or-other": lambda r: r["documentType"] in ("Notice", "Other"),
        "populated-rule-or-proposed": lambda r: r["documentType"] in ("Rule", "Proposed Rule"),
        "populated-2016-or-later": lambda r: r["year"].isdigit() and int(r["year"]) >= 2016,
    }
    drawn: list[dict[str, Any]] = []
    summary: dict[str, int] = {}
    for name, predicate in strata.items():
        rows = sorted((r for r in frame if predicate(r)), key=lambda r: r["rank"])
        summary[name] = len(rows)
        for row in rows[: args.per_stratum]:
            drawn.append({"stratum": name, **row})

    anomaly_counts: Counter[str] = Counter()
    for row in drawn:
        anomaly_counts.update(row["anomalies"] or ["none"])

    payload = {
        "receipt": "regulations-gov-docabstract-display-sample",
        "purpose": (
            "30 real docAbstract values for the v13 display-rule check under text "
            "policy 2.2. Values are stored exactly as found; anomalies are flagged "
            "beside the text and never repaired out of it."
        ),
        "catalog": "~/" + str(args.catalog_root.relative_to(Path.home())),
        "salt": args.salt,
        "draw": "sha256(salt || NUL || documentId), lowest-first within stratum",
        "frame": {
            "documentRows": counts["rows"],
            "populated": counts["populated"],
            "coverage": round(counts["populated"] / max(counts["rows"], 1), 4),
            "stratumPopulations": summary,
        },
        "anomalyVocabulary": {
            "html-tags": "an HTML tag is present in the stored value",
            "html-entities": "an HTML entity such as &amp; or &#39; is present",
            "placeholder": "the whole value is a placeholder such as N/A",
            "identical-to-title": "the value equals the document title exactly",
            "contained-in-title": "the value appears inside the document title",
            "leading-or-trailing-whitespace": "the stored value is not trimmed",
            "carriage-return": "a CR is present, so line endings are not normalised",
            "embedded-newline": "a newline is present inside the trimmed value",
            "all-caps": "the value is entirely upper case",
            "replacement-character": "U+FFFD is present, so an encoding was already lost",
            "very-short": "under 20 characters after trimming",
        },
        "anomalyCounts": dict(anomaly_counts),
        "rows": drawn,
    }
    blob = json.dumps(payload, indent=1, sort_keys=True).encode() + b"\n"
    args.out.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (args.out.parent / (args.out.name + ".sha256")).write_text(f"{digest}  {args.out.name}\n")

    print(f"document rows {counts['rows']:,}   populated {counts['populated']:,}")
    for name, size in summary.items():
        print(f"  {name:28s} population {size:>9,}")
    print(f"\ndrawn {len(drawn)}")
    print("anomalies across the draw:")
    for name, count in anomaly_counts.most_common():
        print(f"  {name:32s} {count}")
    print(f"\nreceipt {args.out}\nsha256  {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
