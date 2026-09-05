"""Draw and fetch the disjoint population a PDF retention floor must be measured on.

Decision 0001's acceptance gates: "every parser declares a retention floor
measured on a named population before it may run". There is no floor for
``application/pdf`` -- the committed calibration carries exactly two,
``application/xml`` and ``text/html`` -- so the attachments campaign would
recover 712,350 files that the builder can enumerate and cannot select as
searchable bodies. This draws the population that closes that.

**Disjoint by construction, because pooling is the defect B5 removed.** The
floor would gate the campaign's corpus: the 790,230 unavailable, unrestricted
documents. So the calibration population is drawn from documents with
disposition ``selected`` -- already carrying renditions, and outside the
campaign by definition. Drawing it from the sample instead would mean the
calibration "could not refuse anything it had not already admitted", which is
what amendment B5 was written to stop. Disjointness is still *measured* on both
document ids and rendition digests rather than argued from the disposition.

**Size 1,000, and the reason is measured rather than conventional.** The margin
rule makes the floor three quarters of the population's observed *minimum*, and
a minimum is the least stable statistic in a sample -- in the committed XML
population the minimum is 0.453 while p01 is 0.7619, so it sits far out in the
tail. Reshuffling both committed 993-document populations 400 times each shows
the running minimum still moving at the 90th percentile of draw position (913
of 993 for XML, 898 of 993 for HTML), and a 250-document stop would have set the
HTML floor at about 0.235 against its real 0.17 -- 38% too strict, refusing
bodies the true floor admits. 1,000 matches the precedent; the settling curve is
reported so a reader can judge stability instead of assuming it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fetch_attachment_sample import BROWSER_UA, _download, _head  # noqa: E402
from tools.select_attachment_sample import _scan_member  # noqa: E402


def draw(
    *,
    catalog_root: Path,
    blob_store: Path,
    salt: str,
    size: int,
    workers: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    manifest = json.loads((catalog_root / "manifests" / "catalog.json").read_text())
    members = sorted(
        (m for m in manifest["members"] if m["role"] == "source-items"),
        key=lambda m: m["blobRef"],
    )
    jobs = [
        (str(blob_store / m["blobRef"].split(":", 1)[1]), salt, f"{i:02d}", "selected-pdf")
        for i, m in enumerate(members)
    ]
    frame: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for kept, member_counts in pool.map(_scan_member, jobs):
            frame.extend(kept)
            counts.update(member_counts)
    frame.sort(key=lambda r: r["rank"])
    return frame[:size], dict(counts)


def fetch(rows: list[dict[str, Any]], *, out_dir: Path, timeout: float, workers: int) -> list[dict[str, Any]]:
    """Fetch each PDF on the unmetered route, judging bytes rather than status."""
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(row: dict[str, Any]) -> dict[str, Any]:
        url = row["pdfUrl"]
        probe = _head(url, timeout)
        record = dict(row, headVerdict=probe["verdict"], headStatus=probe.get("status"))
        if probe["verdict"] != "hit":
            return record
        got = _download(url, timeout, "pdf", out_dir)
        record.update(got)
        return record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--blob-store", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="where to write the draw receipt")
    parser.add_argument("--bytes-dir", type=Path, required=True)
    parser.add_argument("--campaign-frame", type=Path, required=True, help="the sealed sample, for the disjointness check")
    parser.add_argument("--salt", default="docspec-pdf-floor-population-2026-09-05")
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--fetch-workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)

    rows, counts = draw(
        catalog_root=args.catalog_root,
        blob_store=args.blob_store,
        salt=args.salt,
        size=args.size,
        workers=args.workers,
    )
    print(f"selected documents carrying a PDF : {counts.get('selectedWithPdf', 0):,}")
    print(f"selected documents without one    : {counts.get('selectedWithoutPdf', 0):,}")
    print(f"drawn                             : {len(rows):,}")

    campaign = json.loads(args.campaign_frame.read_text())
    campaign_ids = {r["documentId"] for r in campaign["rows"]}
    drawn_ids = {r["documentId"] for r in rows}
    id_overlap = sorted(drawn_ids & campaign_ids)
    print(f"document-id overlap with the sample: {len(id_overlap)}")

    fetched: list[dict[str, Any]] = []
    if not args.no_fetch:
        fetched = fetch(rows, out_dir=args.bytes_dir, timeout=args.timeout, workers=args.fetch_workers)
        ok = [r for r in fetched if r.get("magicOk")]
        print(f"fetched with valid PDF magic      : {len(ok):,} / {len(fetched):,}")

    payload = {
        "receipt": "pdf-retention-floor-population",
        "purpose": (
            "The named, pinned population for an application/pdf document-body "
            "retention floor. Disjoint from the corpus the floor gates."
        ),
        "populationRule": (
            "regulations.gov documents with catalog disposition 'selected' carrying a "
            "PDF fileFormat, ranked by sha256(salt || NUL || documentId), lowest N."
        ),
        "disjointness": {
            "against": "the attachments campaign frame (unavailable, unrestricted)",
            "byConstruction": "disposition 'selected' is excluded from the campaign by definition",
            "documentIdOverlap": len(id_overlap),
            "documentIdOverlapExamples": id_overlap[:10],
            "digestOverlapNote": (
                "rendition-digest disjointness is computed by the calibrator against "
                "the pinned corpus's captured digests, as amendment B5 requires; the "
                "id check here is the cheaper half done at draw time"
            ),
        },
        "size": len(rows),
        "sizeRationale": (
            "The margin rule makes the floor 3/4 of the population minimum. Reshuffling "
            "both committed 993-document populations 400 times each puts the running "
            "minimum's last move at the 90th percentile of draw position (913/993 XML, "
            "898/993 HTML); a 250-document stop would have set the HTML floor 38% too "
            "strict. 1,000 matches the precedent and the settling curve is reported."
        ),
        "salt": args.salt,
        "userAgent": BROWSER_UA,
        "frameCounts": counts,
        "rows": fetched or rows,
    }
    blob = json.dumps(payload, indent=1, sort_keys=True).encode() + b"\n"
    args.out.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (args.out.parent / (args.out.name + ".sha256")).write_text(f"{digest}  {args.out.name}\n")
    print(f"receipt {args.out}")
    print(f"sha256  {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
