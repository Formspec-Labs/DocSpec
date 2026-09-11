"""Saved tasks must use the same worker settings and injected fetcher."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from docspec.cli.execution import _execute_local_task
from docspec.cli.local import _compose_local_run, _load_prepared_local_run, _prepare_local_run, _prepared_tasks
from docspec.cli.requests import _local_run_request
from docspec.cli_io import CliError
from docspec.domain.identity import identity_digest
from docspec.profile_registry import ProfileRegistry
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


class _CountingFetcher(SharedFixtureContentFetcher):
    def __init__(self, root: Path, *, variant: str = "original") -> None:
        super().__init__(root)
        self.configuration_digest = identity_digest({"delegate": self.configuration_digest, "variant": variant})
        self.calls = 0

    def fetch(self, candidate, **kwargs):
        self.calls += 1
        return super().fetch(candidate, **kwargs)


@pytest.fixture
def run_request(tmp_path: Path):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    return _local_run_request(path)


def test_reconstructed_worker_runs_and_recovers_without_fetching_again(run_request) -> None:
    initial = _compose_local_run(run_request, content_fetcher=_CountingFetcher(run_request["roots"]["sourceContent"]))
    prepared = _prepare_local_run(initial, resume=False)
    fetcher = _CountingFetcher(run_request["roots"]["sourceContent"])
    reconstructed = _compose_local_run(run_request, content_fetcher=fetcher)
    recovered = _load_prepared_local_run(reconstructed, prepared.handoff_ref)
    assert recovered == prepared
    task = next(_prepared_tasks(reconstructed, recovered))
    first = _execute_local_task(reconstructed, recovered, task)
    assert fetcher.calls == 1
    again = _load_prepared_local_run(reconstructed, prepared.handoff_ref)
    assert _execute_local_task(reconstructed, again, task) == first
    assert fetcher.calls == 1


@pytest.mark.parametrize("changed", [
    "sourceContent", "fetcherConfiguration", "completedAt", "documentReleaseProducer",
    "sourceCatalogProducer", "partitionPolicyId", "resultSinkId", "reconciliationRoot",
])
def test_changed_worker_refuses_saved_handoff_before_fetch(run_request, tmp_path: Path, changed: str) -> None:
    initial = _compose_local_run(run_request, content_fetcher=_CountingFetcher(run_request["roots"]["sourceContent"]))
    prepared = _prepare_local_run(initial, resume=False)
    updated = {**run_request, "roots": dict(run_request["roots"])}
    if changed == "sourceContent":
        updated["roots"]["sourceContent"] = tmp_path / "other-source-content"
        updated["roots"]["sourceContent"].mkdir()
        (updated["roots"]["sourceContent"] / "document.txt").write_bytes(
            (run_request["roots"]["sourceContent"] / "document.txt").read_bytes()
        )
    elif changed == "reconciliationRoot":
        updated["roots"]["reconciliation"] = tmp_path / "other-reconciliation"
    elif changed == "completedAt":
        updated["completedAt"] = "2026-09-11T15:00:00Z"
    elif changed.endswith("Producer"):
        updated[changed] = replace(
            updated[changed], verifier_implementation_id="git+https://example.test/docspec@" + "2" * 40,
        )
    elif changed in {"partitionPolicyId", "resultSinkId"}:
        updated[changed] += "-changed"
    fetcher = _CountingFetcher(
        updated["roots"]["sourceContent"],
        variant="changed" if changed == "fetcherConfiguration" else "original",
    )
    reconstructed = _compose_local_run(updated, content_fetcher=fetcher)
    with pytest.raises(CliError, match="saved worker composition differs"):
        recovered = _load_prepared_local_run(reconstructed, prepared.handoff_ref)
        _execute_local_task(reconstructed, recovered, next(_prepared_tasks(reconstructed, recovered)))
    assert fetcher.calls == 0


def test_worker_identity_reads_current_fetcher_configuration(run_request) -> None:
    fetcher = _CountingFetcher(run_request["roots"]["sourceContent"])
    composition = _compose_local_run(run_request, content_fetcher=fetcher)
    prepared = _prepare_local_run(composition, resume=False)
    fetcher.configuration_digest = identity_digest({"updated": True})
    with pytest.raises(CliError, match="saved worker composition differs"):
        _load_prepared_local_run(composition, prepared.handoff_ref)
    assert fetcher.calls == 0


def test_fixture_translator_cannot_resume_as_plain_local_fetcher(run_request) -> None:
    translated = _compose_local_run(
        run_request, content_fetcher=SharedFixtureContentFetcher(run_request["roots"]["sourceContent"]),
    )
    prepared = _prepare_local_run(translated, resume=False)
    with pytest.raises(CliError, match="saved worker composition differs"):
        _load_prepared_local_run(_compose_local_run(run_request), prepared.handoff_ref)


@pytest.mark.parametrize("identity", [
    {},
    {"downloader_id": " ", "configuration_digest": identity_digest({})},
    {"downloader_id": "fixture-downloader/v1"},
    {"downloader_id": "fixture-downloader/v1", "configuration_digest": "not-a-digest"},
])
def test_missing_fetcher_identity_refuses_before_local_control_writes(run_request, identity: dict) -> None:
    calls = []
    fetcher = SimpleNamespace(**identity, fetch=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(CliError, match="content fetcher identity is invalid"):
        _compose_local_run(run_request, content_fetcher=fetcher)
    assert calls == []
    assert not run_request["roots"]["controlRepository"].exists()
