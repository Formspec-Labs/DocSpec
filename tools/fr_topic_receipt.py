"""Resolve the Federal Register empty-topic arrays against the live publisher.

SpicySearch decision 0007: *"The Federal Register topic lane remains unqualified
until DocSpec's pinned 30-row live-source receipt resolves the predecessor's
ambiguous empty-topic arrays."* An empty ``sourceObservedTopics`` has two
possible causes and the catalog cannot tell them apart on its own: the publisher
printed no topics, or something between the publisher and the catalog dropped
them. Only the publisher can settle it, so this asks the publisher.

**Why the empty case is observable at all.** ``federalregister.gov/api/v1``
returns ``"topics": []`` rather than omitting the key, verified before this ran.
Against an API that omitted the key, absence and emptiness would be the same
byte and the receipt could not distinguish them.

**The strata are the point.** Ten documents with topics, ten empty after 2000,
and ten empty before 2000, because the three ask different questions. The
populated rows test that topics survive the pipeline intact; the post-2000
empties are the ambiguous case 0007 names; and the pre-2000 empties test the
topic cliff, which an earlier probe attributed to the publisher's own API rather
than to ingest (``receipts/fr-topics-live-vs-parquet-2026-09-02.json``). That
probe compared the publisher to a *parquet*; this one compares the publisher to
the DocSpec catalog, which is the artifact 0007 gates on.

**Shape, not just presence.** Each row records the catalog's
``observedTopicScheme`` and ``observedTopicId`` beside the publisher's string, so
"arrives as the publisher printed it" is checked rather than asserted. The
Federal Register prints a flat list of subject strings and has no thesaurus /
ad-hoc division, so there is no such split to preserve on this source; the row
shape records that rather than leaving it implied.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

FR_SCOPE = "federal-register-documents"
API = "https://www.federalregister.gov/api/v1/documents"
UA = "docspec-topic-receipt/1.0 (+https://github.com/Formspec-Labs/DocSpec)"


def _scan(args: tuple[str, str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reduce one partition to the topic facts, keyed for stratification."""
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
        fact = next(
            (f for f in row.get("sourceNativeFacts") or () if f.get("scopeId") == FR_SCOPE),
            None,
        )
        if fact is None:
            continue
        fields = fact["fields"]
        observed = row.get("sourceObservedTopics") or []
        published = fields.get("publication_date") or ""
        counts["rows"] += 1
        stratum = (
            "populated"
            if observed
            else ("empty-post-2000" if published >= "2000-01-01" else "empty-pre-2000")
        )
        counts[stratum] += 1
        kept.append(
            {
                "documentId": row["documentId"],
                "documentNumber": fields.get("document_number"),
                "publicationDate": published,
                "stratum": stratum,
                "catalogTopics": observed,
                "recordTopics": fields.get("topics"),
                "rank": hashlib.sha256(f"{salt}\x00{row['documentId']}".encode()).hexdigest(),
            }
        )
    return kept, dict(counts)


def fetch_live(document_number: str, timeout: float) -> dict[str, Any]:
    url = f"{API}/{document_number}.json?fields[]=document_number&fields[]=topics&fields[]=publication_date"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            return {
                "status": response.status,
                "topicsKeyPresent": "topics" in payload,
                "liveTopics": payload.get("topics"),
                "livePublicationDate": payload.get("publication_date"),
            }
    except urllib.error.HTTPError as error:
        return {"status": error.code, "topicsKeyPresent": None, "liveTopics": None}
    except (urllib.error.URLError, TimeoutError) as error:
        return {"status": None, "error": type(error).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--blob-store", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--salt", default="docspec-fr-topic-receipt-2026-09-05")
    parser.add_argument("--per-stratum", type=int, default=10)
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=60.0)
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

    print(f"catalog rows        {counts['rows']:,}")
    for stratum in ("populated", "empty-post-2000", "empty-pre-2000"):
        share = 100 * counts[stratum] / max(counts["rows"], 1)
        print(f"  {stratum:18s} {counts[stratum]:>9,}  ({share:5.1f}%)")

    drawn: list[dict[str, Any]] = []
    for stratum in ("populated", "empty-post-2000", "empty-pre-2000"):
        rows = sorted((r for r in frame if r["stratum"] == stratum), key=lambda r: r["rank"])
        drawn.extend(rows[: args.per_stratum])

    print(f"\nfetching {len(drawn)} live at {args.delay}s spacing")
    agree = disagree = 0
    for row in drawn:
        row.update(fetch_live(row["documentNumber"], args.timeout))
        catalog_labels = [t["label"] for t in row["catalogTopics"]]
        live = row.get("liveTopics")
        row["catalogLabels"] = catalog_labels
        row["agrees"] = live is not None and list(live) == catalog_labels
        row["schemes"] = sorted({t["observedTopicScheme"] for t in row["catalogTopics"]})
        row["idEqualsLabel"] = all(t["observedTopicId"] == t["label"] for t in row["catalogTopics"])
        if row["agrees"]:
            agree += 1
        else:
            disagree += 1
        time.sleep(args.delay)

    payload = {
        "receipt": "fr-source-topic-live-30-row",
        "question": (
            "SpicySearch 0007: does an empty sourceObservedTopics mean the publisher "
            "printed no topics, or that something between publisher and catalog dropped them?"
        ),
        "catalog": {
            "root": "~/" + str(args.catalog_root.relative_to(Path.home())),
            "rows": counts["rows"],
            "strata": {k: counts[k] for k in ("populated", "empty-post-2000", "empty-pre-2000")},
        },
        "instrument": (
            f"{API}/{{document_number}}.json?fields[]=topics — no API key, not metered by "
            "api.data.gov, and verified to return \"topics\": [] rather than omitting the key, "
            "so an empty result is observable rather than indistinguishable from absence"
        ),
        "shapeNote": (
            "The Federal Register prints a flat list of subject strings and has no "
            "thesaurus / ad-hoc division, so there is no such split to preserve on this "
            "source. observedTopicScheme is stamped with the source hostname and "
            "observedTopicId is the publisher's string verbatim; both are checked per row."
        ),
        "salt": args.salt,
        "agree": agree,
        "disagree": disagree,
        "rows": drawn,
    }
    blob = json.dumps(payload, indent=1, sort_keys=True).encode() + b"\n"
    args.out.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (args.out.parent / (args.out.name + ".sha256")).write_text(f"{digest}  {args.out.name}\n")
    print(f"\nagree {agree} / {len(drawn)}   disagree {disagree}")
    print(f"receipt {args.out}\nsha256  {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
