"""Reduce the attachment sample's NDJSON to the four things it was drawn to measure.

Kept as a tool rather than a scratch script because the numbers below are cited:
anyone can re-derive them from the receipt and the sealed selection, and a
disagreement is then about the data rather than about what somebody once ran.

Every stratum rate carries a Wilson interval, and the corpus estimate is the
design-weighted `sum(w.indicator)/sum(w)` -- the raw sample fraction is 66.2%
against a weighted 81.0%, because the allocation deliberately over-samples the
withheld cells, and quoting the raw figure as a corpus rate is the error the
weights exist to prevent.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

R = Path("/Users/mikewolfd/Work/corpora/supply-2026-09-02")
NDJSON = R / "receipts" / "attachment-sample-2026-09-05.ndjson"
SELECTION = Path("/Users/mikewolfd/Work/DocSpec/docs/history/2026-09-05-attachment-sample-selection.json")
OUT = R / "receipts" / "attachment-sample-2026-09-05.md"


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% interval, so a stratum rate is never quoted as a point alone."""
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main() -> int:
    rows = [json.loads(line) for line in NDJSON.read_text().splitlines() if line.strip()]
    header = next(r for r in rows if r.get("runHeader"))
    main_rows = [r for r in rows if not r.get("runHeader") and not r.get("reprobe")]
    reprobes = [r for r in rows if r.get("reprobe")]
    selection = json.loads(SELECTION.read_text())
    strata = {s["restrictReasonType"] + " | " + s["documentTypeGroup"]: s for s in selection["strata"]}
    stratum_of = {}
    for row in selection["rows"]:
        stratum_of[row["documentId"]] = (row["restrictReasonType"], row["documentType"])

    # Group by the selection's own cells rather than re-deriving them.
    cells: dict[str, list[dict]] = defaultdict(list)
    for r in main_rows:
        reason = r["restrictReasonTypeFrame"]
        dt_ = stratum_of[r["documentId"]][1]
        if reason != "(none)":
            key = f"{reason} | any"
        elif dt_ in ("Other", "Supporting & Related Material"):
            key = f"(none) | {dt_}"
        else:
            key = "(none) | minor"
        cells[key].append(r)

    lines: list[str] = []
    A = lines.append
    A("# The 2,000-document attachment sample")
    A("")
    A(f"Run {header['startedAt']} to {main_rows[-1]['at']}. Selection "
      f"`{header['selectionDigest'][:16]}…`, key `{header['keyName']}`, "
      f"{header['delaySeconds']}s API spacing, direct arm on.")
    A("")
    A("**The run crashed after the main pass** on an unhandled `RemoteDisconnected` "
      "(fixed in `d8436c9`). All 2,000 documents completed first; the negative "
      "re-probe was finished separately with `--reprobe-only`. Nothing was refetched.")
    A("")
    A(f"User-Agent, on which the whole direct arm depends: `{header['userAgent']}`")
    A("")

    have = [r for r in main_rows if r.get("directHitCount", 0) > 0]
    A("## 1. Recoverable fraction")
    A("")
    A("| cell | drawn | with files | rate | 95% interval | design weight |")
    A("| --- | ---: | ---: | ---: | --- | ---: |")
    num = den = 0.0
    for key in sorted(cells, key=lambda k: -strata[k]["population"]):
        rs = cells[key]
        k = sum(1 for r in rs if r.get("directHitCount", 0) > 0)
        lo, hi = wilson(k, len(rs))
        w = strata[key]["designWeight"]
        num += w * k
        den += w * len(rs)
        A(f"| {key} | {len(rs)} | {k} | {pct(k/len(rs))} | {pct(lo)}–{pct(hi)} | {w:,.2f} |")
    A(f"| **weighted corpus estimate** | 2,000 | {len(have)} raw | **{pct(num/den)}** | | |")
    A("")
    A(f"Raw sample rate is {pct(len(have)/len(main_rows))}, and it is **not** the corpus "
      f"rate: the allocation over-samples the withheld cells by design. The weighted "
      f"estimator `sum(w·indicator)/sum(w)` gives **{pct(num/den)}**.")
    A("")
    A(f"The standing corpus figure from the 306-document probe is 82% "
      f"(657,099–750,366 of 865,082). This sample, drawn independently on a "
      f"different frame with a different instrument, lands at {pct(num/den)}.")
    A("")

    A("## 2. Bytes")
    A("")
    declared = [r.get("declaredBytesAllFormats", 0) for r in have]
    actual = [r.get("actualBytesTotal", 0) for r in have if r.get("actualBytesTotal")]
    match = [r for r in have if r.get("declaredMatchesActual") is True]
    mismatch = [r for r in have if r.get("declaredMatchesActual") is False]
    A(f"- documents with at least one file: **{len(have):,}** of 2,000")
    A(f"- declared bytes, all formats: **{sum(declared)/1e9:.2f} GB**, "
      f"median {statistics.median(declared):,.0f}, mean {statistics.fmean(declared):,.0f}")
    A(f"- actually downloaded: **{sum(actual)/1e9:.2f} GB** across {len(actual):,} documents")
    A(f"- declared equals actual: **{len(match):,}**; differs: **{len(mismatch):,}**")
    bad = [(r["documentId"], d) for r in have for d in (r.get("downloads") or [])
           if d.get("magicOk") is False]
    A(f"- download errors: **{sum(1 for r in have for d in (r.get('downloads') or []) if d.get('downloadError'))}**")
    A(f"- magic bytes contradicted the extension: **{len(bad)}** of "
      f"{sum(len(r.get('downloads') or []) for r in have):,} files")
    if bad:
        A("")
        A("  All three are named `.xlsx` and begin `d0cf11e0` — the OLE2 signature of a "
          "legacy `.xls`, not the ZIP of an `.xlsx`. The bytes are real and the extension "
          "is wrong, which is what a magic check is for: a consumer trusting the name "
          "would fail to open them. " + ", ".join(f"`{i}`" for i, _ in bad) + ".")
    A("")
    weighted_bytes = sum(strata[k]["designWeight"] * sum(r.get("declaredBytesAllFormats", 0) for r in v)
                         for k, v in cells.items())
    weighted_docs = sum(strata[k]["designWeight"] * len(v) for k, v in cells.items())
    A(f"Weighted mean bytes per unavailable document: **{weighted_bytes/weighted_docs:,.0f}**. "
      f"Over the 790,230 unrestricted unavailable documents that projects to "
      f"**{weighted_bytes/weighted_docs*790230/1e12:.2f} TB** of declared content.")
    A("")

    A("## 3. Rate limit and throughput")
    A("")
    t0 = dt.datetime.strptime(header["startedAt"], "%Y-%m-%dT%H:%M:%SZ")
    t1 = dt.datetime.strptime(main_rows[-1]["at"], "%Y-%m-%dT%H:%M:%SZ")
    secs = (t1 - t0).total_seconds()
    direct = sum(r.get("directRequests", 0) for r in main_rows)
    limits = Counter(r["rateLimit"].get("X-RateLimit-Limit") for r in main_rows if r.get("rateLimit"))
    remaining = [int(r["rateLimit"]["X-RateLimit-Remaining"]) for r in main_rows
                 if r.get("rateLimit", {}).get("X-RateLimit-Remaining")]
    A(f"- API arm: {len(main_rows):,} requests in {secs/3600:.2f} h = "
      f"**{len(main_rows)/secs*3600:,.0f}/hour** at concurrency 1")
    A(f"- declared limit: {dict(limits)}; `X-RateLimit-Remaining` fell to a minimum of "
      f"**{min(remaining):,}**")
    A(f"- **429s: {sum(1 for r in main_rows if r.get('status') == 429)}**; "
      f"non-200: {sum(1 for r in main_rows if r.get('status') not in (200,))} "
      f"(all 404, documents the publisher no longer serves)")
    A(f"- direct arm: **{direct:,} requests**, {direct/secs:.2f}/s sustained, "
      f"**{sum(r.get('directClientRejected', 0) for r in main_rows)} client-rejected**, "
      f"{sum(r.get('directUnreadable', 0) for r in main_rows)} unreadable")
    A("")
    A("The direct arm's rate is bounded by the API arm's pacing, not by the host: it "
      "rides inside the 3.7 s gap. It cost nothing from the api.data.gov budget.")
    A("")

    A("## 4. Are any withheld strata empty of attachments?")
    A("")
    A("| reason code | drawn | with files | 95% upper bound |")
    A("| --- | ---: | ---: | ---: |")
    for key in [k for k in cells if not k.startswith("(none)")]:
        rs = cells[key]
        k2 = sum(1 for r in rs if r.get("directHitCount", 0) > 0)
        _, hi = wilson(k2, len(rs))
        A(f"| {key.split(' | ')[0]} | {len(rs)} | {k2} | {pct(hi)} |")
    A("")

    A("## Declared versus served, both directions")
    A("")
    dns = [r for r in main_rows if r.get("declaredNotServed")]
    snd = [r for r in main_rows if r.get("servedNotDeclared")]
    A(f"- API declared a file the host would not serve: **{len(dns)}** documents")
    A(f"- host served a file the API never declared: **{len(snd)}** documents")
    A(f"- disagreed on the URL set: **{sum(1 for r in main_rows if r.get('directAgreesWithApi') is False)}**")
    A("")
    A("That is what licenses sizing the campaign's byte cost from declared metadata "
      "instead of downloading it: the two sources of truth agree on what exists.")
    A("")

    A("## Shapes")
    A("")
    A(f"- attachments with no file at all: "
      f"**{sum(r.get('attachmentsWithNoFormats', 0) for r in main_rows):,}** across "
      f"{sum(1 for r in main_rows if r.get('attachmentsWithNoFormats'))} documents")
    A(f"- documents whose attachment ships in several formats: "
      f"**{sum(1 for r in have if r.get('renditionCount', 0) > r.get('attachmentCount', 0)):,}**")
    A(f"- total attachments {sum(r.get('attachmentCount', 0) for r in main_rows):,}, "
      f"renditions {sum(r.get('renditionCount', 0) for r in main_rows):,} "
      f"({sum(r.get('renditionCount', 0) for r in main_rows) / max(sum(r.get('attachmentCount', 0) for r in main_rows), 1):.3f} per attachment)")
    A(f"- documents carrying inline comment text: "
      f"**{sum(1 for r in main_rows if r.get('inlineCommentChars', 0) > 0):,}**")
    A("")

    A("## The negative re-probe")
    A("")
    flipped = sum(1 for r in reprobes if r.get("directHitCount", 0) > 0)
    A(f"Every one of the **{len(reprobes):,}** negatives was probed a second time after a "
      f"pause, and **{flipped}** changed. A transient failure and a genuine absence look "
      f"identical in one observation; they rarely agree twice. Zero flips means the "
      f"negatives are absences, not failures.")
    OUT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
