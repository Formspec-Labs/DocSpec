"""Shared catalog build and assertion fixtures."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from docspec.adapters.catalog_artifact.builder import SourceCatalogBuilder, SourceCatalogBuildRequest
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.source_catalog import SourceCatalogItem
from tests.support.source_catalog import _FEDERAL_REGISTER_SOURCE, FakeSource, description, producer, record, renditions


def build(root: Path, source: FakeSource):
    """Build the Federal Register catalog with the production builder and return its store and result."""
    store = LocalSourceCatalogStore(root)
    result = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    return store, result


def interpretation_result(item: SourceCatalogItem, kind: str) -> Mapping[str, Any]:
    """Return one interpretation's result, asserting the kind is present."""
    interpretation = next(value for value in item.interpretations if value["interpretationKind"] == kind)
    result = interpretation["result"]
    assert isinstance(result, Mapping)
    return result


def normalization_fields(item: SourceCatalogItem) -> dict[str, Mapping[str, Any]]:
    """Return the normalization interpretation's fields keyed by normalized field name."""
    fields = interpretation_result(item, "normalization")["fields"]
    assert isinstance(fields, tuple)
    return {field["normalizedField"]: field for field in fields}


def assert_no_published_catalog(root: Path) -> None:
    """Assert the root holds no catalog and, if present, an empty staging directory."""
    assert not [path for path in root.iterdir() if path.name != ".staging"]
    staging = root / ".staging"
    if staging.exists():
        assert not tuple(staging.iterdir())


def assert_outside_sentinel_unchanged(root: Path) -> None:
    """Assert a neighboring sentinel file was left untouched."""
    assert [path.name for path in root.iterdir()] == ["sentinel.txt"]
    assert (root / "sentinel.txt").read_bytes() == b"outside must stay unchanged"


def build_with_store(store: LocalSourceCatalogStore) -> None:
    """Build the one-record fixture catalog into an externally supplied store."""
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
