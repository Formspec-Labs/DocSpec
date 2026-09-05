"""Fetch the sealed attachment sample and write one receipt line per item.

Companion to ``select_attachment_sample.py``. It answers the four questions the
sealed selection names, and nothing else: what fraction of unavailable
documents yield content, at what byte cost, under what live rate limit, and
whether any ``restrictReasonType`` stratum is empty of attachments.

**It refuses a selection whose digest has moved.** The sample's authority comes
from being fixed before the fetch, so a run against an edited selection is not
the measurement that was authorized. The digest is checked first, before the key
is read or a socket is opened.

**Concurrency is one, deliberately.** Rate-limit behaviour is a measurement
target, not an obstacle to route around, so the run is strictly sequential and
records every rate-limit header and every 429 with its ``Retry-After``. The
default delay is 3.7 s, which is the published 1,000 requests/hour budget with a
little slack; 2,000 items is therefore roughly two hours. Raising concurrency
would make the rate-limit reading meaningless.

**Bytes are read from the attachment metadata, not downloaded.** Every returned
``fileFormats[].size`` is a declared byte count, which is what a cost estimate
for 712,000 documents needs and costs one request per item instead of two.
``--verify-bytes N`` downloads N of them to check declared against actual,
because a declared size the publisher never validates is exactly the kind of
number that agrees with itself. Default 0: opt in when you want that check.

**The key is read at run time and never leaves this process.** It is sent as the
``X-Api-Key`` header, never as a query parameter, because the URL is written to
every receipt line and a key in a URL would be published with it. Exception text
is scrubbed before it is recorded.

**The response shape here is verified, not assumed.** Ten documents, two from
each stratum, were fetched live on 2026-09-05 to check it before 2,000 rows were
read through this parser. That run corrected two things a reading of the docs
would not have caught: one attachment is commonly published in several formats,
so counting ``fileFormats`` entries overstates attachments and double-counts
bytes; and an attachment can appear in both the linkage and ``included`` while
carrying no file at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_ROOT = "https://api.regulations.gov/v4/documents"
# api.data.gov keys are 40 characters. RefSpec/.env also holds a 41-character
# REGULATIONS_GOV_API_KEY, which the endpoint rejects with API_KEY_INVALID --
# verified live on 2026-09-05 against ten documents, all 403. API_GOV is the
# working key; --key-name overrides if that ever changes.
KEY_NAME = "API_GOV"
# 1,000 requests/hour is the published per-key budget: 3.6 s/request exactly.
DEFAULT_DELAY_SECONDS = 3.7
RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "Retry-After",
)


def read_key(env_path: Path, key_name: str = KEY_NAME) -> str:
    """Return the API key from a dotenv file, without ever printing it."""
    if not env_path.exists():
        raise SystemExit(f"no env file at {env_path}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key_name:
            return value.strip().strip("'\"")
    raise SystemExit(f"{key_name} is not set in {env_path}")


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "<redacted>") if key else text


def _request(url: str, key: str, timeout: float) -> tuple[int, dict[str, str], bytes, float]:
    request = urllib.request.Request(url, headers={"X-Api-Key": key, "Accept": "application/vnd.api+json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            headers = {k: v for k, v in response.headers.items()}
            return response.status, headers, body, (time.monotonic() - started) * 1000
    except urllib.error.HTTPError as error:
        body = error.read()
        headers = {k: v for k, v in error.headers.items()} if error.headers else {}
        return error.code, headers, body, (time.monotonic() - started) * 1000


def _rate_limit(headers: dict[str, str]) -> dict[str, str]:
    lowered = {k.lower(): v for k, v in headers.items()}
    return {name: lowered[name.lower()] for name in RATE_LIMIT_HEADERS if name.lower() in lowered}


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce one document response to the fields the four questions need.

    The shape signals are not decoration. If this parser guesses ``included``
    wrong -- a different key, sizes carried somewhere else -- every row comes
    back ``attachmentCount: 0``, which is indistinguishable from a genuine
    empty and is the reading that would cancel the campaign. So the raw shape
    travels with the count: ``includedTypes`` non-empty beside
    ``attachmentCount: 0`` means the parser missed, not that the document is
    bare, and ``linkedAttachmentCount`` reads the relationship linkage as a
    second, independent signal of how many attachments should have been found.
    """
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}
    comment = attributes.get("comment")
    included = payload.get("included")
    renditions: list[dict[str, Any]] = []
    attachment_count = 0
    empty_attachments = 0
    declared_all = 0
    declared_first = 0
    sizes_missing = 0
    for entry in included or ():
        if entry.get("type") != "attachments":
            continue
        attachment_count += 1
        formats = (entry.get("attributes") or {}).get("fileFormats") or []
        if not formats:
            # Observed live on FDA-1987-N-0054-0051: the attachment is present
            # in both the linkage and `included`, and carries no file at all.
            # "An attachment with nothing to download" and "no attachment" are
            # different answers for the campaign and must not merge.
            empty_attachments += 1
            continue
        for position, fmt in enumerate(formats):
            size = fmt.get("size")
            if isinstance(size, int):
                declared_all += size
                if position == 0:
                    declared_first += size
            else:
                sizes_missing += 1
            renditions.append(
                {
                    "fileUrl": fmt.get("fileUrl"),
                    "format": fmt.get("format"),
                    "size": size,
                }
            )
    linkage = ((data.get("relationships") or {}).get("attachments") or {}).get("data")
    return {
        # One attachment is commonly published in several formats -- a scanned
        # filing arrives as .tif and .pdf of the same pages -- so counting
        # fileFormats entries would overstate attachments and double-count the
        # content. Both counts travel, and so do both byte totals: all formats
        # is the cost of taking everything, first-per-attachment the cost of
        # one copy of each.
        "attachmentCount": attachment_count,
        "renditionCount": len(renditions),
        "attachmentsWithNoFormats": empty_attachments,
        "declaredBytesAllFormats": declared_all,
        "declaredBytesFirstFormatPerAttachment": declared_first,
        "declaredSizesMissing": sizes_missing,
        "renditions": renditions,
        "inlineCommentChars": len(comment) if isinstance(comment, str) else 0,
        "restrictReasonType": attributes.get("restrictReasonType"),
        "documentFileFormatsNull": attributes.get("fileFormats") is None,
        "hasIncludedKey": included is not None,
        "includedTypes": sorted({e.get("type") for e in included or () if e.get("type")}),
        "linkedAttachmentCount": len(linkage) if isinstance(linkage, list) else None,
    }


def _completed_ids(receipt: Path) -> set[str]:
    if not receipt.exists():
        return set()
    done: set[str] = set()
    for line in receipt.read_text().splitlines():
        if not line.strip():
            continue
        try:
            done.add(json.loads(line)["documentId"])
        except (json.JSONDecodeError, KeyError):
            # A torn final line from a killed run: re-fetch that one item.
            continue
    return done


def run(args: argparse.Namespace) -> int:
    payload = args.selection.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    expected = args.expect_digest
    if expected is None:
        sidecar = args.selection.parent / (args.selection.name + ".sha256")
        if not sidecar.exists():
            raise SystemExit(f"no --expect-digest and no sidecar at {sidecar}")
        expected = sidecar.read_text().split()[0]
    if actual != expected:
        raise SystemExit(
            f"selection digest moved: expected {expected}, found {actual}. "
            "This is not the sample that was sealed; re-seal it deliberately or "
            "point at the original."
        )

    selection = json.loads(payload)
    rows = selection["rows"]
    done = _completed_ids(args.receipt)
    pending = [r for r in rows if r["documentId"] not in done]
    print(f"selection   {args.selection} ({actual[:12]}…)")
    print(f"sample      {len(rows)} rows, {len(done)} already done, {len(pending)} pending")
    print(f"receipt     {args.receipt}")
    print(f"pace        {args.delay}s between requests, concurrency 1")
    print(f"estimate    {len(pending) * args.delay / 3600:.1f} h remaining")
    if args.dry_run:
        print("dry-run: no request made")
        return 0

    key = read_key(args.env, args.key_name)
    verified = 0
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open("a") as sink:
        for index, row in enumerate(pending, start=1):
            document_id = row["documentId"]
            url = f"{API_ROOT}/{document_id}?include=attachments"
            record: dict[str, Any] = {
                "documentId": document_id,
                "restrictReasonTypeFrame": row["restrictReasonType"],
                "agencyId": row["agencyId"],
                "designWeight": row["designWeight"],
                "url": url,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                status, headers, body, elapsed = _request(url, key, args.timeout)
                record["status"] = status
                record["elapsedMs"] = round(elapsed, 1)
                record["rateLimit"] = _rate_limit(headers)
                if status == 200:
                    parsed = json.loads(body)
                    if args.save_first_raw and not args.save_first_raw.exists():
                        # One verbatim response, so the shape this parser assumes
                        # can be checked against the publisher's actual reply
                        # before 2,000 rows are read through it. No key appears
                        # in a response body; the key travels in the header.
                        args.save_first_raw.parent.mkdir(parents=True, exist_ok=True)
                        args.save_first_raw.write_bytes(body)
                        print(f"  saved first raw response to {args.save_first_raw}")
                    record.update(_summarize(parsed))
                elif status == 429:
                    # The rate limit is a measurement, so it is recorded before
                    # it is obeyed, and the wait is recorded too.
                    wait = float(record["rateLimit"].get("Retry-After", args.retry_after))
                    record["retryAfterSlept"] = wait
                    sink.write(json.dumps(record, sort_keys=True) + "\n")
                    sink.flush()
                    time.sleep(wait)
                    continue
                else:
                    record["errorBody"] = _scrub(body.decode("utf-8", "replace")[:400], key)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                record["status"] = None
                record["error"] = _scrub(f"{type(error).__name__}: {error}", key)

            if (
                args.verify_bytes
                and verified < args.verify_bytes
                and record.get("renditions")
                and record["renditions"][0].get("fileUrl")
            ):
                target = record["renditions"][0]
                try:
                    with urllib.request.urlopen(target["fileUrl"], timeout=args.timeout) as response:
                        actual_bytes = len(response.read())
                    record["verifiedFirstAttachmentBytes"] = actual_bytes
                    record["declaredMatchesActual"] = actual_bytes == target.get("size")
                except (urllib.error.URLError, TimeoutError) as error:
                    record["verifyError"] = _scrub(f"{type(error).__name__}: {error}", key)
                verified += 1

            sink.write(json.dumps(record, sort_keys=True) + "\n")
            sink.flush()
            if index % 50 == 0:
                print(f"  {index}/{len(pending)}  last status {record.get('status')}")
            time.sleep(args.delay)
    print("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--env", type=Path, default=Path.home() / "Work" / "RefSpec" / ".env")
    parser.add_argument("--key-name", default=KEY_NAME, help=f"env var holding the key (default {KEY_NAME})")
    parser.add_argument("--expect-digest", default=None, help="defaults to the .sha256 sidecar")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retry-after", type=float, default=60.0)
    parser.add_argument("--verify-bytes", type=int, default=0)
    parser.add_argument(
        "--save-first-raw",
        type=Path,
        default=None,
        help="write the first 200 response verbatim, to check the assumed shape",
    )
    parser.add_argument("--dry-run", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
