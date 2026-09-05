"""The docket-side half of the docAbstract display check, joined by documentId.

The distinguishability question is whether a reader can tell a document's own
description from the docket description it inherits, on one page. That needs
both texts side by side, so this carries, for the same 30 documents the first
receipt drew: title, agency, and the docket's ``data/attributes/dkAbstract``.

**A second receipt, not a new version of the first.** The first is sealed at
`sha256:4bcde14a…` and has been cited by that digest. Rewriting it to add
columns would invalidate a citation someone already made, so the two join on
``documentId`` — the same salt and the same rank, carried through — and the
first stays exactly as it was read.

**Docket text is stored exactly as found, like the document text.** A reader
deciding whether two descriptions are distinguishable has to see what is
actually there, including whether the docket's own value is itself a citation, a
title restatement, or empty.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.draw_docabstract_sample import _anomalies  # noqa: E402

DOCUMENT_SCOPE = "regulations-gov-documents"
DOCKET_SCOPE = "regulations-gov-dockets"


def _scan(args: tuple[str, list[str]]) -> list[dict[str, Any]]:
    path, wanted = args
    want = set(wanted)
    found: list[dict[str, Any]] = []
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("documentId") not in want:
            continue
        document = docket = None
        for fact in row.get("sourceNativeFacts") or ():
            attributes = (fact["fields"].get("data") or {}).get("attributes") or {}
            if fact["scopeId"] == DOCUMENT_SCOPE:
                document = attributes
            elif fact["scopeId"] == DOCKET_SCOPE:
                docket = attributes
        document = document or {}
        title = document.get("title")
        docket_abstract = (docket or {}).get("dkAbstract")
        has_docket_text = bool(docket_abstract and str(docket_abstract).strip())
        found.append(
            {
                "documentId": row["documentId"],
                "title": title,
                "agencyId": document.get("agencyId"),
                "docketId": document.get("docketId"),
                "docketFactPresent": docket is not None,
                "docketAbstractPresent": has_docket_text,
                "docketAbstract": str(docket_abstract) if has_docket_text else None,
                "docketAbstractLength": len(str(docket_abstract)) if has_docket_text else 0,
                "docketAbstractAnomalies": (
                    _anomalies(str(docket_abstract), title) if has_docket_text else []
                ),
                "docketAbstractNote": (
                    None
                    if has_docket_text
                    else (
                        "docket record present, dkAbstract empty or null"
                        if docket is not None
                        else "no docket fact joined to this document"
                    )
                ),
                "docketTitle": (docket or {}).get("title"),
            }
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--blob-store", type=Path, required=True)
    parser.add_argument("--first-receipt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args(argv)

    first_bytes = args.first_receipt.read_bytes()
    first_digest = hashlib.sha256(first_bytes).hexdigest()
    first = json.loads(first_bytes)
    ranks = {r["documentId"]: r["rank"] for r in first["rows"]}
    strata = {r["documentId"]: r["stratum"] for r in first["rows"]}
    wanted = list(ranks)

    manifest = json.loads((args.catalog_root / "manifests" / "catalog.json").read_text())
    members = sorted(
        (m for m in manifest["members"] if m["role"] == "source-items"),
        key=lambda m: m["blobRef"],
    )
    jobs = [(str(args.blob_store / m["blobRef"].split(":", 1)[1]), wanted) for m in members]
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for part in pool.map(_scan, jobs):
            rows.extend(part)

    for row in rows:
        row["rank"] = ranks[row["documentId"]]
        row["stratum"] = strata[row["documentId"]]
    rows.sort(key=lambda r: (r["stratum"], r["rank"]))

    missing = sorted(set(wanted) - {r["documentId"] for r in rows})
    with_docket_text = sum(1 for r in rows if r["docketAbstractPresent"])
    anomalies: Counter[str] = Counter()
    for row in rows:
        anomalies.update(row["docketAbstractAnomalies"] or (["none"] if row["docketAbstractPresent"] else []))

    payload = {
        "receipt": "regulations-gov-docabstract-display-sample-companion",
        "purpose": (
            "The docket-side half of the display check: title, agency and the "
            "docket's dkAbstract for the same 30 documents, so a reader can judge "
            "whether a document's own description is distinguishable from the one "
            "it inherits."
        ),
        "joinsTo": {
            "receipt": args.first_receipt.name,
            "sha256": first_digest,
            "on": "documentId, with the first receipt's salt and rank carried through",
            "note": (
                "A second receipt rather than a new version: the first is sealed and "
                "has been cited by digest, and rewriting it would invalidate that citation."
            ),
        },
        "catalog": "~/" + str(args.catalog_root.relative_to(Path.home())),
        "documentsRequested": len(wanted),
        "documentsFound": len(rows),
        "documentsMissing": missing,
        "withDocketAbstract": with_docket_text,
        "withoutDocketAbstract": len(rows) - with_docket_text,
        "docketAbstractAnomalyCounts": dict(anomalies),
        "rows": rows,
    }
    blob = json.dumps(payload, indent=1, sort_keys=True).encode() + b"\n"
    args.out.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (args.out.parent / (args.out.name + ".sha256")).write_text(f"{digest}  {args.out.name}\n")

    print(f"joined {len(rows)} of {len(wanted)}   missing {missing or 'none'}")
    print(f"with a docket description    {with_docket_text}")
    print(f"without one                  {len(rows) - with_docket_text}")
    print("docket-text anomalies:")
    for name, count in anomalies.most_common():
        print(f"  {name:32s} {count}")
    print(f"\nreceipt {args.out}\nsha256  {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
