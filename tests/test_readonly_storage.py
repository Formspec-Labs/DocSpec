"""Read composition uses real adapter constructors without creating saved state."""

from pathlib import Path

import pytest

from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.errors import IntegrityError
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run
from docspec.runtime.storage import _local_profiles, _local_storage
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


@pytest.fixture
def arguments(tmp_path):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    return _local_run_arguments(_local_run_request(path))


def _open(arguments):
    workspace = arguments["workspace"]
    return _local_storage(
        workspace.roots, _local_profiles(arguments["plan"], workspace),
        arguments["document_release_producer"], create=False,
    )


def _snapshot(root: Path):
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*")}


def test_read_composition_does_not_create_staging_in_existing_empty_roots(arguments, tmp_path):
    for name in ("controlRepository", "documentStores", "recordStorage", "blobStorage", "documentCatalog"):
        arguments["workspace"].roots[name].mkdir()
    before = _snapshot(tmp_path)
    _, stores, _, _, _ = _open(arguments)
    assert not stores.has_planned_store_ledger(arguments["plan"].plan_id)
    assert _snapshot(tmp_path) == before


def test_read_composition_refuses_missing_roots_without_creating_them(arguments, tmp_path):
    before = _snapshot(tmp_path)
    with pytest.raises(IntegrityError, match="existing directory"):
        _open(arguments)
    assert _snapshot(tmp_path) == before


def test_read_composition_opens_and_compares_real_retained_output_without_workspace_writes(arguments, tmp_path):
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    with prepare_local_run(**arguments) as prepared:
        run = prepared.run()
        result = prepared.retain(run)
    before = _snapshot(tmp_path)
    controls, stores, records, _, catalog = _open(arguments)
    assert controls.load(run)["selectedItemCount"] == 1
    reader = catalog.open_reader(result)
    assert len(tuple(reader.scan(layer_kind="files"))) == 1
    assert tuple(catalog.compare(result, result, layer_kind="segments")) == ()
    ledger = stores.planned_store_ledger(arguments["plan"].plan_id)
    assert len(tuple(stores.stream_planned_stores(ledger))) == 1
    assert records.max_open_members > 0
    assert _snapshot(tmp_path) == before
