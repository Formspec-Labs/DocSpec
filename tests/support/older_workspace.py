"""Write the DocSpec 0.9.1 workspace fixture, whose bulk state has one ledger row per member.

DocSpec 0.9.1 (38043c4) published a records row and a retention row for every
member of a bulk state; later versions register members per layer and must
keep reading, pinning and protecting through those rows without a migration.
Regenerate only to change the fixture, with the 0.9.1 source on the path:

    git archive 38043c4 src | tar -x -C /tmp/docspec-0.9.1
    PYTHONPATH=/tmp/docspec-0.9.1/src uv run --frozen python tools/with_iceberg.py \\
        python tests/support/older_workspace.py fixtures/core-workspace-0.9.1
"""

from pathlib import Path
import sys

from docspec.domain.identity import canonical_value_bytes, sha256_digest
from docspec.runtime import CoreWorkspace

STATE_ID = "old"
MEMBERS = 8


def occurrence(key):
    """The occurrence identity ``CoreWorkspace.create`` gives one key of the fixture's state."""
    return "urn:docspec:occurrence:" + sha256_digest(canonical_value_bytes([STATE_ID, key]))


def write(path):
    with CoreWorkspace(Path(path)) as workspace:
        workspace.create(STATE_ID, ((f"k{index}", {"index": index}) for index in range(MEMBERS)))


if __name__ == "__main__":
    write(sys.argv[1])
    for lock in Path(sys.argv[1]).glob("ledger.sqlite.*lock"):
        lock.unlink()
