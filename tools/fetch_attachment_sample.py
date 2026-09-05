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

**Bytes are declared by the API and confirmed by the direct arm.** Every
``fileFormats[].size`` is a publisher claim; the direct arm fetches the file and
records the byte count and sha256 it actually got, so the cost estimate rests on
a measurement rather than on a number the publisher never validates. On the ten
documents checked so far the two agreed exactly.

**The key is read at run time and never leaves this process.** It is sent as the
``X-Api-Key`` header, never as a query parameter, because the URL is written to
every receipt line and a key in a URL would be published with it. Exception text
is scrubbed before it is recorded.

**Two arms, and only one of them is metered.** The API arm answers discovery at
concurrency 1 and produces the throughput number for a registered key. The
direct arm probes ``downloads.regulations.gov/{documentId}/attachment_{n}.{ext}``
which serves the same files anonymously and is not metered by api.data.gov, so
it costs nothing from the shared hourly quota and rides inside the API arm's
pacing gap. Each row records whether the two agree.

**The direct arm needs a browser User-Agent and that is not cosmetic.** With
Python's default agent, or curl's, every URL returns 403 with a 919-byte HTML
page -- including files the API declared one second earlier. Verified live on
2026-09-05. Without the header the whole arm reads as "the unmetered route does
not work", which is a clean and completely wrong answer.

**The host's two 403s mean opposite things.** A rejected client gets 403 with
``text/html`` and length 919; a genuinely absent file gets 403 with
``application/xml`` and no length -- confirmed against a nonsense document id.
Reading them alike turns client rejection into evidence of absence, so they are
classified apart, a known-good control URL is interleaved, and a run whose
control goes unhealthy stops rather than recording zeros that look like data.

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
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

API_ROOT = "https://api.regulations.gov/v4/documents"
DOWNLOAD_ROOT = "https://downloads.regulations.gov"
# downloads.regulations.gov refuses Python's default User-Agent and curl's with
# 403 and a 919-byte HTML page -- verified 2026-09-05 on documents whose files
# the API had just declared. With a browser UA the same URLs return 200 and a
# Content-Length matching the API's declared size byte for byte. Without this
# header every probe is a false negative, and the whole direct arm would read as
# "the unmetered route does not work".
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DIRECT_EXTENSIONS = ("pdf", "docx", "xlsx", "doc", "tif")
# A file the API declares and this host serves; interleaved to prove the route
# is healthy, so a run of misses is never mistaken for a run of absences.
CONTROL_URL = f"{DOWNLOAD_ROOT}/DOT-OST-1996-1116-0017/attachment_1.pdf"
# api.data.gov meters govinfo and regulations.gov against ONE hourly budget, and
# this tool alone runs near the whole of it: the sample's first thirty minutes
# took X-RateLimit-Remaining from 999 to 678. On 2026-09-05 a census was
# launched into the running sample and both drew on that budget for nine minutes
# because neither tool looked for the other. This lock is the cheap fix, and it
# is the same file protocol compositions/measure.sh uses: created atomically,
# holding holder, start and purpose, removed when the run ends.
QUOTA_LOCK = Path.home() / "Work" / "corpora" / "supply-2026-09-02" / "compositions" / "api-quota.lock"
MAGIC = {
    "pdf": (b"%PDF",),
    "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",),
    "doc": (b"\xd0\xcf\x11\xe0",),
    "tif": (b"II*\x00", b"MM\x00*"),
}
# api.data.gov keys are 40 characters. API_GOV is the working key; --key-name
# overrides if that ever changes.
#
# RefSpec/.env used to also hold a 41-character REGULATIONS_GOV_API_KEY, which
# this endpoint rejects with API_KEY_INVALID -- verified live on 2026-09-05
# against ten documents, all 403. That name was removed from the file later the
# same day, so the comment's original present tense ("also holds") outlived the
# fact by about ten minutes. The finding is kept rather than deleted with the
# key, because it is the reason not to put a 41-character key back: the length
# is the tell, and a future reader finding one somewhere else should expect 403
# rather than test it against production.
KEY_NAME = "API_GOV"
# 1,000 requests/hour is the published per-key budget: 3.6 s/request exactly.
DEFAULT_DELAY_SECONDS = 3.7
# urllib wraps h.request() in OSError -> URLError, but NOT h.getresponse(), so a
# RemoteDisconnected raised while reading the status line escapes as itself. That
# killed a 2,000-row run on 2026-09-05 after the main pass, mid re-probe. URLError
# and HTTPError are both OSError subclasses, so this tuple is the whole family
# plus the http.client exceptions that are not OSErrors.
NETWORK_ERRORS = (OSError, http.client.HTTPException, TimeoutError)
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


def _head(url: str, timeout: float) -> dict[str, Any]:
    """One HEAD, classified into hit / absent / unreadable.

    The host answers 403 in two different situations and they mean opposite
    things. A rejected client gets 403 with a 919-byte ``text/html`` page; a
    genuinely absent key gets 403 with ``application/xml`` and no length. Reading
    both as "no file" was the failure this classification exists to prevent, and
    only the second is evidence about the document.
    """
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": BROWSER_UA})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "url": url,
                "verdict": "hit" if response.status == 200 else "unreadable",
                "status": response.status,
                "bytes": int(response.headers.get("Content-Length") or 0),
                "contentType": response.headers.get("Content-Type"),
                "ms": round((time.monotonic() - started) * 1000, 1),
            }
    except urllib.error.HTTPError as error:
        content_type = (error.headers.get("Content-Type") or "") if error.headers else ""
        length = (error.headers.get("Content-Length") or "") if error.headers else ""
        absent = error.code in (403, 404) and "xml" in content_type
        rejected = error.code == 403 and "html" in content_type and length == "919"
        return {
            "url": url,
            "verdict": "absent" if absent else ("client-rejected" if rejected else "unreadable"),
            "status": error.code,
            "contentType": content_type or None,
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
    except NETWORK_ERRORS as error:
        return {"url": url, "verdict": "unreadable", "error": type(error).__name__}


def probe_direct(
    document_id: str,
    declared_urls: Sequence[str],
    *,
    timeout: float,
    max_index: int,
    hard_cap: int,
    workers: int,
) -> dict[str, Any]:
    """Probe what the API declared, then a bounded grid for what it did not.

    The declared set comes from this row's own ``fileFormats``, read one second
    earlier, so an unusual extension is fetched rather than guessed at. The grid
    then runs anyway, and anything it finds beyond the declared set is a counted
    disagreement instead of a silent discovery.

    The two directions are kept apart on purpose. *Declared but not served* and
    *served but not declared* are different defects with different consequences,
    and only their combination licenses sizing the campaign's byte cost from
    declared metadata instead of downloading 712,350 files.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        declared = list(pool.map(lambda u: _head(u, timeout), declared_urls))
        requests_made = len(declared)
        declared_hits = [r for r in declared if r["verdict"] == "hit"]

        grid: list[dict[str, Any]] = []
        index = 1
        while index <= hard_cap:
            urls = [f"{DOWNLOAD_ROOT}/{document_id}/attachment_{index}.{ext}" for ext in DIRECT_EXTENSIONS]
            results = list(pool.map(lambda u: _head(u, timeout), urls))
            requests_made += len(results)
            grid.extend(results)
            found = [r for r in results if r["verdict"] == "hit"]
            if index >= max_index and not found:
                break
            index += 1

    grid_hits = [r for r in grid if r["verdict"] == "hit"]
    by_url = {r["url"]: r for r in declared_hits + grid_hits}
    declared_set = set(declared_urls)
    served_set = set(by_url)
    verdicts = [r["verdict"] for r in declared + grid]
    return {
        "userAgent": BROWSER_UA,
        "directHits": sorted(by_url.values(), key=lambda r: r["url"]),
        "directHitCount": len(by_url),
        "directBytes": sum(r.get("bytes", 0) for r in by_url.values()),
        "directRequests": requests_made,
        "directIndicesProbed": min(index, hard_cap),
        "declaredCount": len(declared_set),
        "declaredConfirmedCount": len(declared_set & served_set),
        # Declared but not served: the API promised a file the host will not
        # give up. Sizing cost from declared metadata over-counts by these.
        "declaredNotServed": sorted(declared_set - served_set),
        # Served but not declared: the grid found a file the API never
        # mentioned. Sizing from declared metadata under-counts by these, and
        # an unusual extension shows up here rather than as a false absence.
        "servedNotDeclared": sorted(served_set - declared_set),
        "directAgreesWithApi": declared_set == served_set,
        # Surfaced so a run degraded by client rejection is visible in the data
        # rather than silently indistinguishable from a run of real absences.
        "directClientRejected": verdicts.count("client-rejected"),
        "directUnreadable": verdicts.count("unreadable"),
    }


def _download(url: str, timeout: float, extension: str, sink_dir: Path | None) -> dict[str, Any]:
    """Fetch one file and judge it by its bytes, not by its status line."""
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    digest = hashlib.sha256()
    size = 0
    head = b""
    started = time.monotonic()
    document_id, name = url.rsplit("/", 2)[-2:]
    sink = None
    try:
        if sink_dir is not None:
            target = sink_dir / document_id
            target.mkdir(parents=True, exist_ok=True)
            sink = (target / name).open("wb")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while chunk := response.read(1 << 16):
                if not head:
                    head = chunk[:8]
                digest.update(chunk)
                size += len(chunk)
                if sink is not None:
                    sink.write(chunk)
    except NETWORK_ERRORS as error:
        return {"url": url, "downloadError": type(error).__name__}
    finally:
        if sink is not None:
            sink.close()
    expected = MAGIC.get(extension, ())
    return {
        "url": url,
        "actualBytes": size,
        "sha256": digest.hexdigest(),
        # A 919-byte error page written to disk still has a 200-shaped story to
        # tell; its first bytes do not.
        "magicOk": any(head.startswith(m) for m in expected) if expected else None,
        "magicHead": head[:4].hex(),
        "ms": round((time.monotonic() - started) * 1000, 1),
    }


@contextmanager
def api_quota_lock(path: Path, *, holder: str, purpose: str, wait: bool, poll: float = 15.0):
    """Hold the shared api.data.gov budget for the metered arm of this run.

    Only the API arm needs it. The direct route is unmetered, so a run that skips
    the API half needs no lock and does not take one.
    """
    if path is None:
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = (
        f"holder={holder} pid {os.getpid()}\n"
        f"start={started}\n"
        f"purpose={purpose}\n"
    )
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current = path.read_text().splitlines()[:1]
            if not wait:
                raise SystemExit(
                    f"the api.data.gov quota lock is held: {current}. "
                    f"Wait, or pass --no-quota-lock if you know the holder is finished."
                ) from None
            print(f"  api quota lock held by {current}, waiting {poll:.0f}s")
            time.sleep(poll)
            continue
        with os.fdopen(handle, "w") as sink:
            sink.write(body)
        break
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


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


def reprobe_only(args: argparse.Namespace) -> int:
    """Finish the negative re-probe for a receipt whose main pass is complete.

    The protocol re-probes every negative once, because a transient failure and a
    genuine absence look identical in one observation. A run that dies partway
    through that pass leaves the main data whole and the protocol half-kept, and
    resuming the main loop will not finish it -- every document is already
    recorded, so no negatives are collected to retry. This closes that gap
    without re-fetching anything that succeeded. Direct route only: no metered
    request, no quota, no lock.
    """
    rows = [json.loads(line) for line in args.receipt.read_text().splitlines() if line.strip()]
    main = [r for r in rows if not r.get("runHeader") and not r.get("reprobe")]
    already = {r["documentId"] for r in rows if r.get("reprobe")}
    pending = [r for r in main if not r.get("directHitCount") and r["documentId"] not in already]
    print(f"negatives {sum(1 for r in main if not r.get('directHitCount')):,}, "
          f"already re-probed {len(already):,}, pending {len(pending):,}")
    if args.dry_run or not pending:
        return 0
    print(f"pausing {args.control_pause}s before the retry, as the protocol requires")
    time.sleep(args.control_pause)
    flipped = 0
    with args.receipt.open("a") as sink:
        for index, row in enumerate(pending, start=1):
            again = probe_direct(
                row["documentId"],
                [r["fileUrl"] for r in row.get("renditions", ()) if r.get("fileUrl")],
                timeout=args.timeout,
                max_index=args.direct_max_index,
                hard_cap=args.direct_hard_cap,
                workers=args.direct_workers,
            )
            if again["directHitCount"]:
                flipped += 1
            sink.write(
                json.dumps(
                    {
                        "documentId": row["documentId"],
                        "reprobe": True,
                        "reprobePass": "resumed",
                        "firstHitCount": row.get("directHitCount"),
                        **again,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            sink.flush()
            if index % 100 == 0:
                print(f"  {index}/{len(pending)}")
    print(f"re-probe flipped {flipped} of {len(pending)}")
    return 0


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
    print(f"pace        {'API arm off (unmetered only)' if args.no_api else f'{args.delay}s between API requests, concurrency 1'}")
    print(f"direct      {'off' if args.no_direct else f'on, {args.direct_workers} workers, unmetered'}")
    print(f"estimate    {len(pending) * args.delay / 3600:.1f} h remaining")
    if args.dry_run:
        print("dry-run: no request made")
        return 0

    quota_lock = None if (args.no_quota_lock or args.no_api) else args.quota_lock
    key = "" if args.no_api else read_key(args.env, args.key_name)
    control_failed = False
    direct_requests = 0
    negatives: list[tuple[str, dict[str, Any]]] = []
    wall_started = time.monotonic()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with (
        api_quota_lock(
            quota_lock,
            holder="fetch_attachment_sample",
            purpose=f"attachment sample, {len(pending)} rows at {args.delay}s",
            wait=not args.no_quota_wait,
        ),
        args.receipt.open("a") as sink,
    ):
        # A run header, not a row. The route's health depends on the exact
        # User-Agent, so it is recorded with the run rather than left implicit in
        # whatever the code said that day. Rows are identified by documentId, so
        # the resume scan skips this line.
        sink.write(
            json.dumps(
                {
                    "runHeader": True,
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "selectionDigest": actual,
                    "userAgent": BROWSER_UA,
                    "keyName": args.key_name,
                    "delaySeconds": args.delay,
                    "directEnabled": not args.no_direct,
                    "directExtensions": list(DIRECT_EXTENSIONS),
                    "directMaxIndex": args.direct_max_index,
                    "controlUrl": CONTROL_URL,
                    "controlEvery": args.control_every,
                    "pending": len(pending),
                },
                sort_keys=True,
            )
            + "\n"
        )
        sink.flush()
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
            if args.no_api:
                # Direct arm only: no metered request, so no declared URLs and
                # the grid does the whole job. Recorded as such rather than left
                # to look like an API result.
                record["status"] = None
                record["apiSkipped"] = "no-api"
                record.update(
                    probe_direct(
                        document_id,
                        [],
                        timeout=args.timeout,
                        max_index=args.direct_max_index,
                        hard_cap=args.direct_hard_cap,
                        workers=args.direct_workers,
                    )
                )
                direct_requests += record.get("directRequests", 0)
                sink.write(json.dumps(record, sort_keys=True) + "\n")
                sink.flush()
                continue
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
            except (*NETWORK_ERRORS, json.JSONDecodeError) as error:
                record["status"] = None
                record["error"] = _scrub(f"{type(error).__name__}: {error}", key)


            if not args.no_direct and record.get("status") == 200:
                if index % args.control_every == 1 or control_failed:
                    control = _head(CONTROL_URL, args.timeout)
                    record["controlVerdict"] = control["verdict"]
                    if control["verdict"] != "hit":
                        # The route is unhealthy. Every direct verdict from here
                        # would be a false absence, so the run stops rather than
                        # filling the receipt with zeros that look like data.
                        control_failed = True
                        record["directSkipped"] = "control unhealthy"
                        sink.write(json.dumps(record, sort_keys=True) + "\n")
                        sink.flush()
                        print(f"  control URL returned {control['verdict']}; pausing {args.control_pause}s")
                        time.sleep(args.control_pause)
                        recheck = _head(CONTROL_URL, args.timeout)
                        if recheck["verdict"] != "hit":
                            raise SystemExit(
                                "control URL still unhealthy after a pause. The direct route is "
                                "not readable from here; every probe would be a false negative. "
                                "Re-run to resume -- completed rows are kept."
                            )
                        control_failed = False
                        continue
                    control_failed = False
                declared_urls = [
                    r["fileUrl"] for r in record.get("renditions", ()) if r.get("fileUrl")
                ]
                record.update(
                    probe_direct(
                        document_id,
                        declared_urls,
                        timeout=args.timeout,
                        max_index=args.direct_max_index,
                        hard_cap=args.direct_hard_cap,
                        workers=args.direct_workers,
                    )
                )
                direct_requests += record.get("directRequests", 0)
                if record["directHitCount"] == 0:
                    negatives.append((document_id, record))
                elif args.download:
                    downloads = []
                    for hit in record["directHits"]:
                        extension = hit["url"].rsplit(".", 1)[-1]
                        downloads.append(_download(hit["url"], args.timeout, extension, args.bytes_dir))
                    record["downloads"] = downloads
                    record["actualBytesTotal"] = sum(d.get("actualBytes", 0) for d in downloads)
                    record["declaredMatchesActual"] = record["actualBytesTotal"] == record["directBytes"]
                    record["allMagicOk"] = all(d.get("magicOk") is not False for d in downloads)

            sink.write(json.dumps(record, sort_keys=True) + "\n")
            sink.flush()
            if index % 50 == 0:
                print(f"  {index}/{len(pending)}  last status {record.get('status')}")
            time.sleep(args.delay)

    # Every negative is re-probed once after a pause. A transient throttle and a
    # genuine absence look identical in one observation; they rarely agree twice.
    if negatives and not args.no_direct:
        print(f"re-probing {len(negatives)} negatives after {args.control_pause}s")
        time.sleep(args.control_pause)
        flipped = 0
        with args.receipt.open("a") as sink:
            for document_id, previous in negatives:
                again = probe_direct(
                    document_id,
                    [r["fileUrl"] for r in previous.get("renditions", ()) if r.get("fileUrl")],
                    timeout=args.timeout,
                    max_index=args.direct_max_index,
                    hard_cap=args.direct_hard_cap,
                    workers=args.direct_workers,
                )
                direct_requests += again.get("directRequests", 0)
                if again["directHitCount"] > 0:
                    flipped += 1
                sink.write(
                    json.dumps(
                        {
                            "documentId": document_id,
                            "reprobe": True,
                            "firstHitCount": previous.get("directHitCount"),
                            **again,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                sink.flush()
        print(f"re-probe flipped {flipped} of {len(negatives)} negatives to hits")

    elapsed = time.monotonic() - wall_started
    print(f"done in {elapsed / 3600:.2f} h")
    if args.no_api:
        print(f"api requests   0 (API arm off; {len(pending)} rows probed on the unmetered route only)")
    else:
        print(f"api requests   {len(pending)} at {len(pending) / max(elapsed, 1):.2f}/s")
    print(f"direct requests {direct_requests} at {direct_requests / max(elapsed, 1):.2f}/s")
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
    parser.add_argument(
        "--save-first-raw",
        type=Path,
        default=None,
        help="write the first 200 response verbatim, to check the assumed shape",
    )
    parser.add_argument("--quota-lock", type=Path, default=QUOTA_LOCK)
    parser.add_argument("--no-quota-lock", action="store_true", help="skip the shared api.data.gov lock")
    parser.add_argument("--no-quota-wait", action="store_true", help="fail instead of waiting when the lock is held")
    parser.add_argument("--no-api", action="store_true", help="direct arm only; takes no quota and no lock")
    parser.add_argument("--no-direct", action="store_true", help="skip the unmetered downloads.regulations.gov arm")
    parser.add_argument("--download", action="store_true", default=True)
    parser.add_argument("--no-download", dest="download", action="store_false")
    parser.add_argument("--bytes-dir", type=Path, default=None, help="keep downloaded files here (default: discard after hashing)")
    parser.add_argument("--direct-max-index", type=int, default=3)
    parser.add_argument("--direct-hard-cap", type=int, default=25)
    parser.add_argument("--direct-workers", type=int, default=5)
    parser.add_argument("--control-every", type=int, default=25)
    parser.add_argument("--control-pause", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reprobe-only",
        action="store_true",
        help="finish the negative re-probe for an existing receipt; no metered requests",
    )
    args = parser.parse_args(argv)
    return reprobe_only(args) if args.reprobe_only else run(args)


if __name__ == "__main__":
    sys.exit(main())
