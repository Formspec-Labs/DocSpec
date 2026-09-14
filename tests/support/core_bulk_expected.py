"""Independent full-workload C03 answers; no SQL or experiment algorithms.

Run after the measured stage, in a separate process:
  uv run --no-sync python -m tests.support.core_bulk_expected fields /tmp/core-c03
  uv run --no-sync python -m tests.support.core_bulk_expected whole /tmp/core-c03

Regenerate the pinned fixture, apply the eager reference selection semantics,
and compare its complete framed digest with the measured receipt. Whole rows
are streamed in independently computed order without retaining their 8 GiB
values. This checker is specific to core-bulk-v1, not a production shortcut.
"""

import argparse
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

from docspec.domain.identity import canonical_value_bytes
from tests.support.core_reference import selected_value
from tests.support.core_workload import CORE_MEMBER_COUNT, core_value


def expected_rows(mode: str, count: int):
    if mode == "whole":
        # Every value's first property is body, whose first block dominates
        # canonical byte order. Equal fixture pairs then sort by their keys.
        # Check the complete byte ordering below so collisions cannot invalidate
        # that shortcut silently. Only ordinals and small sort keys materialize.
        def order(ordinal):
            source = ordinal - ordinal % 2 if ordinal < 2048 else ordinal
            return hashlib.sha256(f"core-bulk-v1:{source}:0".encode()).digest(), ordinal

        previous = b""
        for ordinal in sorted(range(count), key=order):
            row = canonical_value_bytes([
                "present", selected_value(core_value(ordinal)), ["key", f"{ordinal:07d}"],
            ])
            assert previous <= row, "fixture prefix ordering did not establish full canonical order"
            yield row
            previous = row
    else:
        selectors = [{"label": "url", "pointer": "/url"}, {"label": "metadata", "pointer": "/metadata/field"}]
        population = min(count, 1024) if mode == "named" else count
        rows = [canonical_value_bytes(["present", selected_value(core_value(i), selectors)]) for i in range(population)]
        if mode == "named":
            rows.extend([canonical_value_bytes(["absent"])] * (1025 - population))
        yield from sorted(rows)


def expected_digest(mode: str, count: int) -> str:
    digest = hashlib.sha256(b'["docspec-selected-members",1,[')
    for index, row in enumerate(expected_rows(mode, count)):
        if index:
            digest.update(b",")
        digest.update(row)
    digest.update(b"]]")
    return "sha256:" + digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fields", "whole", "named"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--count", type=int, default=CORE_MEMBER_COUNT)
    parser.add_argument("--receipt", type=Path, help="Explicit measured receipt, including comparison-only runs")
    args = parser.parse_args()
    started = time.perf_counter()
    expected = expected_digest(args.mode, args.count)
    receipt = args.receipt or args.directory / f"{args.mode}.json"
    actual = json.loads(receipt.read_text())["comparison"]["digest"]
    record = {
        "mode": args.mode, "count": args.count, "expected": expected, "actual": actual,
        "matches": expected == actual, "elapsed_seconds": time.perf_counter() - started,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
    }
    with (args.directory / f"{args.mode}-expected.json").open("x") as output:
        output.write(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2), flush=True)
    assert expected == actual, "full selected values differ from the independent fixture answer"


if __name__ == "__main__":
    main()
