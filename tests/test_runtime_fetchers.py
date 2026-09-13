"""Actual local transport works for supplied inputs and refuses changed recovery settings."""

import pytest

from docspec.adapters.content_fetchers import LocalFileContentFetcher, RoutingContentFetcher
from docspec.domain.identity import sha256_digest
from docspec.domain.plans import WorkLimits
from docspec.errors import IntegrityError
from docspec.runtime import build_local_catalog, open_local_inspection, prepare_local_experiment
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from tests.helpers import document_release_producer
from tests.support.source_catalog import producer


@pytest.fixture
def configured(tmp_path):
    content = b"Local contribution notes."
    content_root = tmp_path / "input"
    content_root.mkdir()
    (content_root / "notes.txt").write_bytes(content)
    workspace = LocalWorkspace(tmp_path / "dataset", {"sourceContent": content_root})
    namespace = "urn:test:local-records"
    source = SuppliedRecordSource(({
        "recordId": "notes", "sourceIssuedVersion": "revision1", "title": "Notes", "metadata": {},
        "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/plain", "immutable-object", "notes.txt",
            expected_sha256=sha256_digest(content), expected_byte_size=len(content),
        ).to_dict()],
    },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
        max_records=1, max_bytes=4096)
    catalog = build_local_catalog((source,), workspace,
        policy=SuppliedRecordCatalogPolicy(namespace, "1"), catalog_id="urn:test:local-catalog",
        producer=producer(), max_scratch_bytes=8 * 1024**2)
    local = LocalFileContentFetcher(content_root)
    settings = {
        "limits": WorkLimits(1, 4096, 1, 1, 1, 8192, 60, 1),
        "source_catalog_producer": producer(), "document_release_producer": document_release_producer(),
        "completed_at": "2026-09-11T12:00:00Z", "deadline_epoch_seconds": 4_000_000_000,
        "stop_after": "capture", "content_fetcher": RoutingContentFetcher(local=local),
    }
    return catalog.reference, workspace, local, settings


def test_local_candidate_without_transport_pin_retains_its_actual_observation_and_recovers(configured):
    source, workspace, _, settings = configured
    with prepare_local_experiment(source, workspace, **settings) as prepared:
        run = prepared.run()
        release = prepared.retain(run)
        assert prepared.run() == run
        view = open_local_inspection(prepared.plan, workspace,
            document_release_producer=settings["document_release_producer"], release_ref=release)
        captured = tuple(view.records("files"))[0]["payload"]
        assert captured["transportVersion"].startswith("local-stat:")
        assert captured["downloaderId"] == settings["content_fetcher"].downloader_id
        assert captured["downloaderConfigurationDigest"] == settings["content_fetcher"].configuration_digest
        assert view.summary()["work"]["counts"]["capturedFiles"] == 1


@pytest.mark.parametrize("zero_tasks", [False, True])
def test_changed_route_settings_refuse_recovery_even_without_a_fetch(configured, zero_tasks):
    source, workspace, local, settings = configured
    if zero_tasks:
        settings["selection"] = {"includeItemIds": []}
    with prepare_local_experiment(source, workspace, **settings) as prepared:
        prepared.run()
        assert prepared.handoff.expected_task_count == (0 if zero_tasks else 1)
        local.chunk_size //= 2
        with pytest.raises(IntegrityError, match="settings changed"):
            prepared.run()
        with pytest.raises(IntegrityError):
            prepare_local_experiment(source, workspace, handoff_ref=prepared.handoff_ref, **settings)
