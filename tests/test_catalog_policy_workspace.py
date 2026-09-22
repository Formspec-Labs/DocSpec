"""SQLite catalog-policy workspace: exact keys, canonical order and scratch caps.

Payloads round-trip verbatim under namespaced keys in canonical UTF-16 order, and the
scratch allowance is checked before creating or reopening a database.
"""

from pathlib import Path
import sqlite3

import pytest

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.errors import IntegrityError, LimitExceededError


def test_workspace_round_trips_exact_keys_and_isolates_namespaces(tmp_path: Path) -> None:
    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        workspace.put("first", ("same",), {"value": 1})
        workspace.put("second", ("same",), {"value": 2})

        assert workspace.get("first", ("same",)) == {"value": 1}
        assert workspace.get("second", ("same",)) == {"value": 2}
        assert workspace.get("first", ("missing",)) is None


def test_workspace_uses_canonical_utf16_tuple_order(tmp_path: Path) -> None:
    """Keys iterate in canonical UTF-16 tuple order, where U+10000 sorts before U+E000."""
    keys = (
        ("\ue000",),
        ("\U00010000",),
        ("a", "b"),
        ("aa",),
        ("a",),
    )
    expected = (
        ("a",),
        ("a", "b"),
        ("aa",),
        ("\U00010000",),
        ("\ue000",),
    )
    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        for key in keys:
            workspace.put("ordered", key, {"key": list(key)})

        assert [tuple(value["key"]) for value in workspace.iter_ordered("ordered")] == list(
            expected
        )


def test_workspace_refuses_key_replacement(tmp_path: Path) -> None:
    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        workspace.put("items", ("one",), {"value": 1})

        with pytest.raises(IntegrityError, match="already exists"):
            workspace.put("items", ("one",), {"value": 2})

        assert workspace.get("items", ("one",)) == {"value": 1}


def test_the_payload_fast_path_stores_and_streams_the_exact_bytes(tmp_path) -> None:
    """put_payload/iter_payloads are a trust seam; pin its two properties.

    The bytes come back verbatim, in ordered-key order, identical to what the
    checked put() path would have stored for the same values -- and the fast
    path refuses a duplicate key exactly as the checked path does. (Codex's
    wheel-check flagged this seam as tested only indirectly.)
    """

    import pytest
    from docspec.errors import IntegrityError
    from rulespec_artifacts import canonical_json_bytes

    from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace

    values = [
        {"sourceItemId": "b", "n": 2},
        {"sourceItemId": "a", "n": 1},
        {"sourceItemId": "c", "nested": {"z": 1, "a": [1, 2]}},
    ]
    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as fast, SqliteCatalogPolicyWorkspace(
        directory=tmp_path
    ) as checked:
        for value in values:
            fast.put_payload("rows", (value["sourceItemId"],), canonical_json_bytes(value))
            checked.put("rows", (value["sourceItemId"],), value)
        fast_bytes = list(fast.iter_payloads("rows"))
        checked_values = list(checked.iter_ordered("rows"))
        assert fast_bytes == [canonical_json_bytes(value) for value in checked_values]
        assert [value["sourceItemId"] for value in checked_values] == ["a", "b", "c"]
        with pytest.raises(IntegrityError, match="already exists"):
            fast.put_payload("rows", ("a",), b"{}")


def test_workspace_scratch_cap_refuses_growth_and_removes_temporary_state(tmp_path):
    """Exceeding the scratch allowance raises and closing removes every file it wrote."""
    allowance = 512 * 1024
    with SqliteCatalogPolicyWorkspace(directory=tmp_path, max_scratch_bytes=allowance) as workspace:
        workspace.put("small", ("one",), {"value": 1})
        workspace.commit()
        with pytest.raises(LimitExceededError, match="scratch allowance"):
            workspace.put("large", ("one",), {"value": "x" * allowance})
        assert sum(path.stat().st_size for path in tmp_path.rglob("*") if path.is_file()) <= allowance
    assert list(tmp_path.iterdir()) == []


def test_workspace_checks_cap_before_creation_and_cleans_connection_failure(tmp_path, monkeypatch):
    """A cap below the minimum SQLite size and a failed connection both leave the directory empty."""
    with pytest.raises(LimitExceededError, match="minimum SQLite"):
        SqliteCatalogPolicyWorkspace(directory=tmp_path, max_scratch_bytes=1)
    assert list(tmp_path.iterdir()) == []

    def cannot_connect(*args):
        raise sqlite3.OperationalError("controlled connection failure")

    monkeypatch.setattr(sqlite3, "connect", cannot_connect)
    with pytest.raises(sqlite3.OperationalError, match="controlled"):
        SqliteCatalogPolicyWorkspace(directory=tmp_path, max_scratch_bytes=512 * 1024)
    assert list(tmp_path.iterdir()) == []


def test_resume_refuses_an_existing_database_larger_than_the_allowance(tmp_path):
    """A smaller allowance refuses an existing database, and the retained bytes stay readable."""
    path = tmp_path / "retained.sqlite3"
    with SqliteCatalogPolicyWorkspace(path=path) as workspace:
        workspace.put("large", ("one",), {"value": "x" * 1024**2})
        workspace.commit()
    with pytest.raises(LimitExceededError, match="existing catalog workspace"):
        SqliteCatalogPolicyWorkspace(path=path, max_scratch_bytes=512 * 1024)
    with SqliteCatalogPolicyWorkspace(path=path) as reopened:
        assert len(reopened.get("large", ("one",))["value"]) == 1024**2
