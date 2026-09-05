"""Count the exact string shapes three readers named, over the whole catalog.

The readers set the boundaries; this only counts. Each predicate below is a
literal string test stated in full, so a disagreement about a number is a
disagreement about a predicate rather than about a judgement, and re-running it
re-derives the same figure.

The docket predicates are reported twice — once over distinct dockets and once
weighted by the documents that inherit them — because a docket description is
inherited by every document in its docket, so the two answer different
questions. "How many dockets say `Subject:` plus their own title" sizes the
publisher's habit; "how many documents are searched on such a string" sizes the
effect on retrieval, and it is the second that decides whether a display rule is
worth writing.
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

DOCUMENT_SCOPE = "regulations-gov-documents"
DOCKET_SCOPE = "regulations-gov-dockets"

# Predicate 1, as the readers wrote it: the value BEGINS with either prefix,
# case as printed. " FR " is the page-citation marker inside such a value.
FR_PREFIXES = ("Federal Register of ", "Federal Register for ")
FR_PAGE = " FR "

# Predicate 2. The needle is the readers' phrase; the BLOCK is the fixed EPA
# template that phrase sits in, measured from three real values:
#   "Please contact the EPA Docket Center, Public Reading Room to view this
#    document. Address: ... Telephone: ... Fax: ... Email:
#    docket-customerservice@epa.gov."
# "Text outside the block" is what remains once that template is removed and
# whitespace is collapsed. Defining the block by its own end anchor rather than
# by a character budget means a value that merely happens to be long is not
# counted as carrying extra text.
EPA_NEEDLE = "EPA Docket Center, Public Reading Room"
EPA_BLOCK = re.compile(
    r"Please contact the EPA Docket Center,\s*Public Reading Room.*?"
    r"docket-customerservice@epa\.gov\.?",
    re.IGNORECASE | re.DOTALL,
)

# Predicate 3.
SUBJECT_PREFIX = "Subject:"
CONTACT_PREFIX = "Contact:"
WHITESPACE = re.compile(r"\s+")


def normalize(value: str) -> str:
    return WHITESPACE.sub(" ", value).strip()


def _scan(path: str) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    counts: Counter[str] = Counter()
    dockets: dict[str, dict[str, Any]] = {}
    residuals: list[int] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        document = docket = None
        for fact in row.get("sourceNativeFacts") or ():
            attributes = (fact["fields"].get("data") or {}).get("attributes") or {}
            if fact["scopeId"] == DOCUMENT_SCOPE:
                document = attributes
            elif fact["scopeId"] == DOCKET_SCOPE:
                docket = attributes
        document = document or {}
        counts["documents"] += 1

        value = document.get("docAbstract")
        if value and str(value).strip():
            text = str(value)
            counts["docAbstractPresent"] += 1
            if text.startswith(FR_PREFIXES):
                counts["p1_fr_prefix"] += 1
                if FR_PAGE in text:
                    counts["p1_fr_prefix_with_page_citation"] += 1
            if EPA_NEEDLE in text:
                counts["p2_epa_reading_room"] += 1
                residual = normalize(EPA_BLOCK.sub("", text))
                residuals.append(len(residual))
                if residual:
                    counts["p2_with_text_outside_block"] += 1
                    if len(residual) >= 20:
                        counts["p2_with_20_or_more_chars_outside"] += 1

        # Docket side: counted per document here, and deduplicated to dockets by
        # the caller, so both denominators come from one pass.
        docket_id = document.get("docketId")
        docket_abstract = (docket or {}).get("dkAbstract")
        if docket_id and docket_abstract and str(docket_abstract).strip():
            text = str(docket_abstract)
            title = (docket or {}).get("title")
            flags = []
            if text.lstrip().startswith(SUBJECT_PREFIX):
                flags.append("subject")
            if text.lstrip().startswith(CONTACT_PREFIX):
                flags.append("contact")
            if title and normalize(text) == normalize(f"{SUBJECT_PREFIX} {title}"):
                flags.append("subject-equals-title")
            counts["documentsWithDocketAbstract"] += 1
            for flag in flags:
                counts[f"p3_docWeighted_{flag}"] += 1
            dockets[docket_id] = {"flags": flags}
    return {"counts": dict(counts), "dockets": dockets, "residuals": residuals}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--blob-store", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    manifest = json.loads((args.catalog_root / "manifests" / "catalog.json").read_text())
    members = sorted(
        (m for m in manifest["members"] if m["role"] == "source-items"),
        key=lambda m: m["blobRef"],
    )
    jobs = [str(args.blob_store / m["blobRef"].split(":", 1)[1]) for m in members]
    counts: Counter[str] = Counter()
    dockets: dict[str, dict[str, Any]] = {}
    residuals: list[int] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for part in pool.map(_scan, jobs):
            counts.update(part["counts"])
            dockets.update(part["dockets"])
            residuals.extend(part["residuals"])

    docket_flags: Counter[str] = Counter()
    for record in dockets.values():
        for flag in record["flags"]:
            docket_flags[flag] += 1
    residuals.sort()

    payload = {
        "receipt": "description-prevalence",
        "purpose": (
            "Prevalence of the exact string shapes three readers named on the "
            "30-row display page. The readers set the boundary; this counts it."
        ),
        "catalog": "~/" + str(args.catalog_root.relative_to(Path.home())),
        "predicates": {
            "1": {
                "field": "document data/attributes/docAbstract",
                "test": 'value.startswith("Federal Register of ") or value.startswith("Federal Register for ")',
                "caseSensitive": True,
                "refinement": 'of those, value contains " FR "',
                "count": counts["p1_fr_prefix"],
                "withPageCitation": counts["p1_fr_prefix_with_page_citation"],
                "shareOfPopulatedDocAbstract": round(
                    counts["p1_fr_prefix"] / max(counts["docAbstractPresent"], 1), 6
                ),
            },
            "2": {
                "field": "document data/attributes/docAbstract",
                "test": 'value contains "EPA Docket Center, Public Reading Room"',
                "blockDefinition": (
                    "the fixed EPA template matched by "
                    r"/Please contact the EPA Docket Center,\s*Public Reading Room"
                    r".*?docket-customerservice@epa\.gov\.?/is, removed, then "
                    "whitespace collapsed; whatever remains is text outside the block"
                ),
                "count": counts["p2_epa_reading_room"],
                "withAnyTextOutsideBlock": counts["p2_with_text_outside_block"],
                "with20OrMoreCharsOutsideBlock": counts["p2_with_20_or_more_chars_outside"],
                "residualLengthPercentiles": {
                    "min": residuals[0] if residuals else 0,
                    "median": residuals[len(residuals) // 2] if residuals else 0,
                    "p95": residuals[min(int(0.95 * len(residuals)), len(residuals) - 1)] if residuals else 0,
                    "max": residuals[-1] if residuals else 0,
                },
            },
            "3": {
                "field": "docket data/attributes/dkAbstract",
                "tests": {
                    "subject": 'value.lstrip().startswith("Subject:")',
                    "contact": 'value.lstrip().startswith("Contact:")',
                    "subject-equals-title": (
                        'collapse-whitespace(value) == collapse-whitespace("Subject: " + docket title)'
                    ),
                },
                "byDocket": {
                    "distinctDocketsWithAbstract": len(dockets),
                    "subject": docket_flags["subject"],
                    "contact": docket_flags["contact"],
                    "subjectEqualsTitle": docket_flags["subject-equals-title"],
                },
                "byDocumentInheriting": {
                    "documentsWithDocketAbstract": counts["documentsWithDocketAbstract"],
                    "subject": counts["p3_docWeighted_subject"],
                    "contact": counts["p3_docWeighted_contact"],
                    "subjectEqualsTitle": counts["p3_docWeighted_subject-equals-title"],
                },
            },
        },
        "denominators": {
            "documents": counts["documents"],
            "documentsWithDocAbstract": counts["docAbstractPresent"],
            "distinctDocketsWithAbstract": len(dockets),
            "documentsInheritingADocketAbstract": counts["documentsWithDocketAbstract"],
        },
    }
    blob = json.dumps(payload, indent=1, sort_keys=True).encode() + b"\n"
    args.out.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (args.out.parent / (args.out.name + ".sha256")).write_text(f"{digest}  {args.out.name}\n")

    p = payload["predicates"]
    print(f"documents {counts['documents']:,}   with docAbstract {counts['docAbstractPresent']:,}")
    print(f"\n1  Federal Register of/for prefix   {p['1']['count']:,}")
    print(f"     of those containing ' FR '      {p['1']['withPageCitation']:,}")
    print(f"\n2  EPA reading-room boilerplate     {p['2']['count']:,}")
    print(f"     with any text outside the block {p['2']['withAnyTextOutsideBlock']:,}")
    print(f"     with >=20 chars outside         {p['2']['with20OrMoreCharsOutsideBlock']:,}")
    print(f"\n3  dockets with a description       {len(dockets):,}")
    print(f"     Subject: prefix                 {docket_flags['subject']:,} dockets / {counts['p3_docWeighted_subject']:,} documents")
    print(f"     Contact: prefix                 {docket_flags['contact']:,} dockets / {counts['p3_docWeighted_contact']:,} documents")
    print(f"     Subject: + own title            {docket_flags['subject-equals-title']:,} dockets / {counts['p3_docWeighted_subject-equals-title']:,} documents")
    print(f"\nreceipt {args.out}\nsha256  {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
