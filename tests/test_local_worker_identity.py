"""Saved tasks must use the same worker settings and injected fetcher."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.errors import IntegrityError, ProfileError
from docspec.runtime import prepare_local_run
from docspec.workspace import LocalWorkspace
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
    return _local_run_arguments(_local_run_request(path))


def test_reconstructed_worker_runs_and_recovers_without_fetching_again(run_request) -> None:
    prepared = prepare_local_run(
        **run_request, content_fetcher=_CountingFetcher(run_request["workspace"].roots["sourceContent"]), resume=False,
    )
    fetcher = _CountingFetcher(run_request["workspace"].roots["sourceContent"])
    recovered = prepare_local_run(**run_request, content_fetcher=fetcher, handoff_ref=prepared.handoff_ref)
    assert recovered == prepared
    task = next(recovered.task_source(recovered.handoff))
    first = recovered.execute_task(recovered.handoff, task)
    assert fetcher.calls == 1
    again = prepare_local_run(**run_request, content_fetcher=fetcher, handoff_ref=prepared.handoff_ref)
    assert again.execute_task(again.handoff, task) == first
    assert fetcher.calls == 1


@pytest.mark.parametrize("changed", [
    "sourceContent", "fetcherConfiguration", "completed_at", "document_release_producer",
    "source_catalog_producer", "partition_policy_id", "result_sink_id", "reconciliationRoot",
])
def test_changed_worker_refuses_saved_handoff_before_fetch(run_request, tmp_path: Path, changed: str) -> None:
    prepared = prepare_local_run(
        **run_request, content_fetcher=_CountingFetcher(run_request["workspace"].roots["sourceContent"]), resume=False,
    )
    updated = dict(run_request)
    roots = run_request["workspace"].roots
    if changed == "sourceContent":
        roots["sourceContent"] = tmp_path / "other-source-content"
        roots["sourceContent"].mkdir()
        (roots["sourceContent"] / "document.txt").write_bytes(
            (run_request["workspace"].roots["sourceContent"] / "document.txt").read_bytes()
        )
    elif changed == "reconciliationRoot":
        roots["reconciliation"] = tmp_path / "other-reconciliation"
    elif changed == "completed_at":
        updated["completed_at"] = "2026-09-11T15:00:00Z"
    elif changed.endswith("_producer"):
        updated[changed] = replace(
            updated[changed], verifier_implementation_id="git+https://example.test/docspec@" + "2" * 40,
        )
    elif changed in {"partition_policy_id", "result_sink_id"}:
        updated[changed] += "-changed"
    fetcher = _CountingFetcher(
        roots["sourceContent"],
        variant="changed" if changed == "fetcherConfiguration" else "original",
    )
    updated["workspace"] = LocalWorkspace(run_request["workspace"].root, roots, run_request["workspace"].profile_directory)
    with pytest.raises(IntegrityError, match="saved worker composition differs"):
        recovered = prepare_local_run(**updated, content_fetcher=fetcher, handoff_ref=prepared.handoff_ref)
        recovered.execute_task(recovered.handoff, next(recovered.task_source(recovered.handoff)))
    assert fetcher.calls == 0


def test_worker_identity_reads_current_fetcher_configuration(run_request) -> None:
    fetcher = _CountingFetcher(run_request["workspace"].roots["sourceContent"])
    prepared = prepare_local_run(**run_request, content_fetcher=fetcher, resume=False)
    fetcher.configuration_digest = identity_digest({"updated": True})
    with pytest.raises(IntegrityError, match="saved worker composition differs"):
        prepare_local_run(**run_request, content_fetcher=fetcher, handoff_ref=prepared.handoff_ref)
    assert fetcher.calls == 0


def test_fixture_translator_cannot_resume_as_plain_local_fetcher(run_request) -> None:
    prepared = prepare_local_run(
        **run_request, content_fetcher=SharedFixtureContentFetcher(run_request["workspace"].roots["sourceContent"]),
        resume=False,
    )
    with pytest.raises(IntegrityError, match="saved worker composition differs"):
        prepare_local_run(**run_request, handoff_ref=prepared.handoff_ref)


@pytest.mark.parametrize("identity", [
    {},
    {"downloader_id": " ", "configuration_digest": identity_digest({})},
    {"downloader_id": "fixture-downloader/v1"},
    {"downloader_id": "fixture-downloader/v1", "configuration_digest": "not-a-digest"},
])
def test_missing_fetcher_identity_refuses_before_local_control_writes(run_request, identity: dict) -> None:
    calls = []
    fetcher = SimpleNamespace(**identity, fetch=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ProfileError, match="content fetcher identity is invalid"):
        prepare_local_run(**run_request, content_fetcher=fetcher)
    assert calls == []
    assert not run_request["workspace"].roots["controlRepository"].exists()


@pytest.mark.parametrize("field", [
    "downloader_id", "downloader_configuration_digest", "task_id", "attempt_id", "transport_version",
])
def test_fetch_receipt_mismatch_closes_source_before_reading_bytes(run_request, field: str) -> None:
    from docspec.adapters.storage import LocalDocumentStoreRepository
    from docspec.domain.jobs import FailureClass
    from docspec.ports.content_fetcher import FetchStream

    class LyingFetcher(_CountingFetcher):
        def __init__(self, root: Path):
            super().__init__(root)
            self.closed = 0
            self.reads = 0

        def fetch(self, candidate, **kwargs):
            stream = super().fetch(candidate, **kwargs)
            value = identity_digest({"wrong": True}) if field == "downloader_configuration_digest" else "wrong"

            def chunks():
                self.reads += 1
                yield from stream.chunks

            def close():
                self.closed += 1
                stream.close()

            return FetchStream(replace(stream.metadata, **{field: value}), chunks(), close)

    fetcher = LyingFetcher(run_request["workspace"].roots["sourceContent"])
    prepared = prepare_local_run(**run_request, content_fetcher=fetcher)
    task = next(prepared.task_source(prepared.handoff))
    result = prepared.execute_task(prepared.handoff, task)
    assert fetcher.calls == fetcher.closed == 1
    assert fetcher.reads == 0
    store = LocalDocumentStoreRepository(run_request["workspace"].roots["documentStores"]).load(result.output_store)
    assert not store.entries[0].captured_files
    assert store.entries[0].failures[0].failure_class is FailureClass.ARTIFACT_INTEGRITY


def test_mutated_fetcher_cannot_run_under_prepared_identity(run_request) -> None:
    from docspec.adapters.storage import LocalDocumentStoreRepository
    from docspec.domain.jobs import FailureClass

    fetcher = _CountingFetcher(run_request["workspace"].roots["sourceContent"])
    prepared = prepare_local_run(**run_request, content_fetcher=fetcher)
    fetcher.configuration_digest = identity_digest({"changed-after-prepare": True})
    result = prepared.execute_task(prepared.handoff, next(prepared.task_source(prepared.handoff)))
    assert fetcher.calls == 0
    store = LocalDocumentStoreRepository(run_request["workspace"].roots["documentStores"]).load(result.output_store)
    assert not store.entries[0].captured_files
    assert store.entries[0].failures[0].failure_class is FailureClass.ARTIFACT_INTEGRITY
