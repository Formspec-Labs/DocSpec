"""A workspace DocSpec 0.9.1 wrote, with one ledger row per bulk state member, keeps working unmigrated.

The fixture was written by the 0.9.1 source itself (see tests/support/older_workspace.py), so these
checks do not rely on today's ledger writer to reproduce the old rows.
"""

from pathlib import Path
import shutil

from docspec.domain import core
from docspec.runtime import CoreWorkspace
from tests.support.older_workspace import MEMBERS, STATE_ID, occurrence

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "core-workspace-0.9.1"
STATE_KEYS = [("state", STATE_ID), ("state_representation", STATE_ID + ":physical")]


def rows_by_kind(workspace):
    with workspace.ledger._transaction() as connection:
        return dict(connection.execute("SELECT kind,count(*) FROM records GROUP BY kind").fetchall())


def remove(workspace, update_id, keys):
    policy = core.RetentionPolicy(format_version=1, policy_id=update_id + "-policy",
                                  description={"remove": [list(key) for key in keys], "collect_unreferenced": False})
    workspace.retain([policy], unit_id=update_id + "-policy", roots=[("retention_policy", policy.policy_id)])
    return workspace.maintenance.remove_under_policy(update_id, policy.policy_id, keys)


def test_member_rows_from_0_9_1_read_pin_and_protect_until_removed(tmp_path, monkeypatch):
    path = tmp_path / "workspace"
    shutil.copytree(FIXTURE, path)
    with CoreWorkspace(path, create=False) as workspace:
        assert rows_by_kind(workspace) == {"entity": MEMBERS, "state": 1, "state_representation": 1}
        searched = []
        find = workspace.states.find_members
        monkeypatch.setattr(workspace.states, "find_members",
                            lambda session, identities, **kwargs: searched.append(set(identities)) or find(session, identities, **kwargs))
        with workspace.publisher.session() as session:
            row = next(session.read_records([("entity", occurrence("k3"))]))[0]
        assert row.value.value.value == {"index": 3} and row.retained and row.available
        assert workspace.retain([], unit_id="reference", roots=[("entity", occurrence("k3"))])
        assert searched == []
        workspace.create("new", [("key", {"n": 1})])
        assert rows_by_kind(workspace)["entity"] == MEMBERS
        with workspace.publisher.session() as session:
            layer = workspace.states.layers(session, STATE_ID)["entities"].reference
            files = list(workspace.records.physical_references(layer))
        # The old rows still protect their layer after the state goes.
        remove(workspace, "state", STATE_KEYS)
        assert all((workspace.records.root / ref.locator).exists() for ref in files)
        with workspace.publisher.session() as session:
            rows = [row for batch in session.read_records(("entity", occurrence(f"k{index}")) for index in range(MEMBERS))
                    for row in batch]
        assert [row.value.value.value["index"] for row in rows] == list(range(MEMBERS))
        assert all(row.retained and row.available for row in rows)
        # Releasing them is an explicit removal of those rows.
        remove(workspace, "rows", [("entity", occurrence(f"k{index}")) for index in range(MEMBERS)])
        assert not any((workspace.records.root / ref.locator).exists() for ref in files)
        removed = [row for batch in workspace.ledger.retained_records(kind="entity") for row in batch]
        assert len(removed) == MEMBERS and not any(row.available for row in removed)
