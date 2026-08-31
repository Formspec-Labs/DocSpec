from pathlib import Path

import pytest

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.errors import IntegrityError


def test_workspace_round_trips_exact_keys_and_isolates_namespaces(tmp_path: Path) -> None:
    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        workspace.put("first", ("same",), {"value": 1})
        workspace.put("second", ("same",), {"value": 2})

        assert workspace.get("first", ("same",)) == {"value": 1}
        assert workspace.get("second", ("same",)) == {"value": 2}
        assert workspace.get("first", ("missing",)) is None


def test_workspace_uses_canonical_utf16_tuple_order(tmp_path: Path) -> None:
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
