"""Draw a sealed, deterministic, stratified sample of unavailable documents.

The measured sample that should precede the ~712,000-document attachments
campaign, so its yield and cost are known before authorization. Selection ranks
rows by ``sha256(salt || documentId)`` and takes the lowest per
``(restrictReasonType, documentType group)`` cell; the frame filters
``disposition == "unavailable"`` under catalog policy 1.2.0, which drops the 41
publisher test fixtures without a name pattern of its own. Allocation is
deliberately disproportionate, so every row carries a ``designWeight`` and the
raw sample fraction must not be read as a corpus rate. Run with
``--catalog-root``, ``--blob-store`` and ``--out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.catalog_sample_support import (  # noqa: E402
    iter_source_item_rows,
    source_item_member_paths,
    report_catalog_root,
)

DOCUMENT_SCOPE = "regulations-gov-documents"
NO_REASON = "(none)"

# Cells are (restrictReasonType, documentType group), because that is the axis
# the 712,350 estimate is weighted on and the axis where its uncertainty lives.
# A census of the frame on 2026-09-05:
#
#   Other                          694,914 unrestricted +     299 restricted
#   Supporting & Related Material   93,179 unrestricted +  74,578 restricted
#   Notice / Proposed Rule / Rule / Public Submission  2,137 +      99
#
# Two facts drive the allocation. Nearly every restricted document is Supporting
# & Related Material (74,578 of 74,976), and `Other` -- two thirds of the whole
# unavailable population -- is 99.96% unrestricted.
#
# So the dominant uncertainty in the corpus estimate is `Other` unrestricted:
# 694,914 documents at a rate measured on 120 probes (93.3%, 87.4-96.6%), which
# is +/-32,000 documents. Supporting & Related unrestricted adds +/-10,000 on 65
# probes. The restricted block contributes almost nothing either way -- 0 of 45
# Copyrighted and 0 of 3 CBI have files -- so re-measuring it buys little.
#
# An earlier draw here spent half the sample on the restricted strata. That only
# re-confirmed a measured zero; these weights move those rows to where the
# estimate is actually loose, and keep enough in each reason code to bound its
# zero at roughly 2-6%.
MAIN_TYPES = ("Other", "Supporting & Related Material")

ALLOCATION: dict[tuple[str, str], int] = {
    (NO_REASON, "Other"): 1050,
    (NO_REASON, "Supporting & Related Material"): 450,
    (NO_REASON, "minor"): 150,
    ("Copyrighted", "any"): 120,
    ("Other", "any"): 120,
    ("Confidential Business Information", "any"): 60,
    ("Personally Identifiable Information", "any"): 50,
}


def _cell(row: dict[str, Any]) -> tuple[str, str]:
    """Place a row in its (reason, documentType group) cell."""
    reason = row["restrictReasonType"]
    if reason != NO_REASON:
        # The restricted block is 99.5% one document type, so splitting it by
        # type would make cells of three rows. It stays whole per reason code.
        return (reason, "any")
    document_type = row["documentType"]
    if document_type in MAIN_TYPES:
        return (reason, document_type)
    return (reason, "minor")


def _rank(salt: str, document_id: str) -> str:
    return hashlib.sha256(f"{salt}\x00{document_id}".encode()).hexdigest()


def _document_fact(row: dict[str, Any]) -> dict[str, Any] | None:
    for fact in row.get("sourceNativeFacts") or ():
        if fact.get("scopeId") == DOCUMENT_SCOPE:
            return fact
    return None


def _scan_member(args: tuple[str, str, str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reduce one partition blob to the unavailable-document sampling frame."""
    path, salt, partition_id = args
    kept: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in iter_source_item_rows(path):
        counts["rowsRead"] += 1
        disposition = (row.get("selection") or {}).get("disposition")
        counts[f"disposition/{disposition}"] += 1
        if disposition != "unavailable":
            continue
        fact = _document_fact(row)
        if fact is None:
            # A non-document scope cannot be fetched from the documents
            # endpoint. Counted rather than dropped silently.
            counts["unavailableWithoutDocumentFact"] += 1
            continue
        attributes = fact["fields"]["data"]["attributes"]
        relationships = fact["fields"]["data"].get("relationships") or {}
        attachments = (relationships.get("attachments") or {}).get("links") or {}
        comment = attributes.get("comment")
        reason = attributes.get("restrictReasonType") or NO_REASON
        counts[f"reason/{reason}"] += 1
        kept.append(
            {
                "documentId": row["documentId"],
                "agencyId": attributes.get("agencyId"),
                "documentType": attributes.get("documentType"),
                "subtype": attributes.get("subtype"),
                "restrictReasonType": reason,
                "objectId": attributes.get("objectId"),
                "attachmentsUrl": attachments.get("related"),
                "fileFormatsNull": attributes.get("fileFormats") is None,
                "inlineCommentChars": len(comment) if isinstance(comment, str) else 0,
                "rank": _rank(salt, row["documentId"]),
                "partitionId": partition_id,
            }
        )
    return kept, dict(counts)


def _largest_remainder(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Apportion `total` across `weights` deterministically, every key >= 1.

    Ties break on the key, so two runs over the same frame apportion the same
    way. Keys are dropped only when `total` is smaller than the key count, and
    then the lowest-population keys go first.
    """
    keys = sorted(weights, key=lambda k: (-weights[k], k))
    if total < len(keys):
        keys = keys[:total]
    pool = sum(weights[k] for k in keys)
    if pool == 0:
        return {}
    base = {k: 1 for k in keys}
    remaining = total - len(keys)
    exact = {k: remaining * weights[k] / pool for k in keys}
    floors = {k: int(exact[k]) for k in keys}
    used = sum(floors.values())
    order = sorted(keys, key=lambda k: (-(exact[k] - floors[k]), k))
    for key in order[: remaining - used]:
        floors[key] += 1
    return {k: base[k] + floors[k] for k in keys}


def build_selection(
    *,
    catalog_root: Path,
    blob_store: Path,
    salt: str,
    allocation: dict[tuple[str, str], int],
    workers: int,
) -> dict[str, Any]:
    jobs = [
        (path, salt, f"{index:02d}")
        for index, path in enumerate(source_item_member_paths(catalog_root, blob_store))
    ]

    frame: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for kept, member_counts in pool.map(_scan_member, jobs):
            frame.extend(kept)
            counts.update(member_counts)

    by_cell: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in frame:
        by_cell[_cell(row)].append(row)

    strata: list[dict[str, Any]] = []
    chosen: list[dict[str, Any]] = []
    for cell in sorted(allocation, key=lambda c: (-len(by_cell.get(c, ())), c)):
        rows = by_cell.get(cell, [])
        want = min(allocation[cell], len(rows))
        by_agency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_agency[row["agencyId"] or "(none)"].append(row)
        quota = _largest_remainder(want, {a: len(v) for a, v in by_agency.items()})
        taken: list[dict[str, Any]] = []
        for agency in sorted(quota):
            ordered = sorted(by_agency[agency], key=lambda r: r["rank"])
            taken.extend(ordered[: quota[agency]])
        weight = len(rows) / len(taken) if taken else 0.0
        for row in taken:
            row["designWeight"] = weight
        chosen.extend(taken)
        strata.append(
            {
                "restrictReasonType": cell[0],
                "documentTypeGroup": cell[1],
                "population": len(rows),
                "requested": allocation[cell],
                "drawn": len(taken),
                "designWeight": weight,
                "agenciesInStratum": len(by_agency),
                "agenciesDrawn": len(quota),
                "populationWithInlineComment": sum(1 for r in rows if r["inlineCommentChars"] > 0),
                "drawnWithInlineComment": sum(1 for r in taken if r["inlineCommentChars"] > 0),
            }
        )

    chosen.sort(key=lambda r: (r["restrictReasonType"], r["documentType"] or "", r["rank"]))
    catalog_label = report_catalog_root(catalog_root)
    return {
        "receipt": "attachment-sample-selection",
        "question": (
            "Before authorizing the ~712,000-document attachments campaign: what "
            "fraction of unavailable regulations.gov documents actually yield "
            "content, at what byte cost per item, under what live rate limit, and "
            "are any restrictReasonType strata empty of attachments entirely?"
        ),
        "measures": [
            "recoverableFraction: per stratum, and corpus-wide via designWeight",
            "bytesPerItem: declared fileFormats[].size on returned attachments",
            "rateLimit: observed headers and 429s at concurrency 1",
            "emptyStrata: restrictReasonType strata returning zero attachments",
        ],
        "frame": {
            "catalogRoot": catalog_label,
            "policyVersion": "1.2.0",
            "rowsRead": counts["rowsRead"],
            "byDisposition": {
                k.split("/", 1)[1]: v for k, v in sorted(counts.items()) if k.startswith("disposition/")
            },
            "unavailableWithDocumentFact": len(frame),
            "unavailableWithoutDocumentFact": counts.get("unavailableWithoutDocumentFact", 0),
            "note": (
                "This catalog is not publishable: it pins the pre-composite "
                "Federal Register release (DocSpec 0003). It is a sound sampling "
                "frame because the Federal Register identity does not touch a "
                "regulations.gov document's withholding reason or agency."
            ),
        },
        "design": {
            "salt": salt,
            "rank": "sha256(salt || NUL || documentId), lowest-first within stratum",
            "primaryStratum": "(restrictReasonType, documentType group)",
            "secondaryStratum": "agencyId, apportioned by largest remainder, minimum 1",
            "allocation": {f"{k[0]} | {k[1]}": v for k, v in allocation.items()},
            "disproportionate": True,
            "estimator": (
                "Corpus-wide rates are sum(designWeight * indicator) / "
                "sum(designWeight). The raw sample fraction over-weights the "
                "small withheld strata by roughly two orders of magnitude and "
                "must not be read as a corpus rate."
            ),
            "powerNote": (
                "At n=200 with zero attachments observed, the one-sided 95% bound "
                "on that stratum's true attachment rate is about 1.5%. At the "
                "proportional n=3 the same zero would bound nothing."
            ),
        },
        "strata": strata,
        "sampleSize": len(chosen),
        "rows": chosen,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--blob-store", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--salt", default="docspec-attachment-sample-2026-09-05")
    parser.add_argument("--workers", type=int, default=min(14, os.cpu_count() or 4))
    args = parser.parse_args(argv)

    selection = build_selection(
        catalog_root=args.catalog_root,
        blob_store=args.blob_store,
        salt=args.salt,
        allocation=ALLOCATION,
        workers=args.workers,
    )
    payload = json.dumps(selection, indent=1, sort_keys=True).encode() + b"\n"
    args.out.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (args.out.parent / (args.out.name + ".sha256")).write_text(f"{digest}  {args.out.name}\n")

    print(f"selection      {args.out}")
    print(f"sha256         {digest}")
    print(f"sample size    {selection['sampleSize']}")
    for stratum in selection["strata"]:
        label = f"{stratum['restrictReasonType']} | {stratum['documentTypeGroup']}"
        print(
            f"  {label:52s} pop={stratum['population']:>7,} drawn={stratum['drawn']:>4} "
            f"weight={stratum['designWeight']:>10.2f} agencies={stratum['agenciesDrawn']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
