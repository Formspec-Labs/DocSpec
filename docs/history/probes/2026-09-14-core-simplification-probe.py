"""Small paired diagnostic; supply a fresh workspace and select code via PYTHONPATH.

Run each revision in a separate process with the same interpreter. Times include
retention and evidence computation; the all-value oracle runs outside the timer.
This is not a capacity or engine-comparison benchmark.
"""

import json
from pathlib import Path
import sys
from time import perf_counter

from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes
from docspec.runtime import CoreWorkspace


def value(index):
    return {
        "url": f"https://example.test/{index}",
        "body": 'a\\b"c\n' * 512,
        "n": index,
    }


def main():
    root, count = Path(sys.argv[1]), 8192
    result = {
        "rows": count,
        "input_bytes": sum(len(canonical_value_bytes(value(i))) for i in range(count)),
        "stages": {},
    }
    with CoreWorkspace(root) as workspace:
        start = perf_counter()
        workspace.create("root", ((str(i), value(i)) for i in range(count)))
        result["stages"]["build"] = {"seconds": perf_counter() - start}
        with workspace.publisher.session() as session:
            selectors = [
                ("fields", core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),))),
                ("whole", core.Whole()),
            ]
            for label, selector in selectors:
                definition = core.StateMembers(member_selector=selector)
                start = perf_counter()
                selected = workspace.selections.retain(
                    session, selected_value_id=label, definition=definition,
                    origin=core.Origin(parent_entity_id="root"),
                )
                evidence = workspace.selections.evidence(session, selected)
                result["stages"][label] = {
                    "seconds": perf_counter() - start,
                    "digest": evidence.digest,
                    "bytes": evidence.byte_size,
                }
                rows = list(workspace.selections.rows(session, selected))
                assert len(rows) == count
                expected = []
                for i in range(count):
                    selected_value = (["present", value(i)] if label == "whole" else
                                      [["url", "present", value(i)["url"]]])
                    expected.append(canonical_value_bytes(["present", selected_value]))
                assert sorted(row[2] for row in rows) == sorted(expected)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
