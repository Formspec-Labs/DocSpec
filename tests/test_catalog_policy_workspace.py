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
