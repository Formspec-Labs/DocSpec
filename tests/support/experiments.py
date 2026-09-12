"""Shared bounded local experiments for repair and retained-evidence tests."""

from dataclasses import replace

import pytest

from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import FailureClass
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import open_local_inspection, prepare_local_experiment
from docspec.workspace import LocalWorkspace
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.support.processors import _CountingExtractor, _CountingProcessor, _CountingSegmenter
from tests.support.profiles import _seeded_local_run


class _FailingProcessor(_CountingProcessor):
    def __init__(self, description, *, error=ValueError, fail_on=1):
        super().__init__(description)
        self.error, self.fail_on = error, fail_on
        self.attempts = 0
        self.fail = True

    def process(self, request, payload, prerequisites):
        self.attempts += 1
        if self.fail and self.attempts >= self.fail_on:
            raise self.error("fixture failure")
        return super().process(request, payload, prerequisites)


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    request, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    original = _local_run_arguments(_local_run_request(request))
    workspace = original["workspace"]
    content = b"First paragraph.\n\nSecond paragraph."
    (workspace.roots["sourceContent"] / "document.txt").write_bytes(content)
    source = write_shared_source_catalog(workspace.roots["sourceCatalog"], (SourceItem(
        "document-a", "two-paragraphs", (CandidateFile(
            "primary", "document.txt", "text/plain", expected_size=len(content),
            expected_digest=sha256_digest(content), transport_version="fixture:v2",
        ),), metadata={"expectedSegments": 2},
    ),))
    fetcher = SharedFixtureContentFetcher(workspace.roots["sourceContent"])
    fetches = []
    fetch = fetcher.fetch

    def counted(candidate, **kwargs):
        fetches.append(candidate.candidate_id)
        return fetch(candidate, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", counted)
    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0)
    extractor, segmenter = _CountingExtractor(TextExtractor()), _CountingSegmenter(ParagraphSegmenter())
    settings = {
        "limits": replace(original["plan"].limits, max_attempts=1),
        "retry_policy": retry,
        "accepted_failure_policy": AcceptedFailurePolicy(accepted_classes=(
            FailureClass.DETERMINISTIC_INPUT, FailureClass.TRANSIENT_EXTERNAL,
        )),
        "source_catalog_producer": original["source_catalog_producer"],
        "document_release_producer": original["document_release_producer"],
        "completed_at": original["completed_at"],
        "deadline_epoch_seconds": original["deadline_epoch_seconds"],
        "content_fetcher": fetcher, "extractor": extractor, "segmenter": segmenter,
    }

    def run(*, processors=(), fresh=False, on_prepared=None, **changes):
        selected_workspace = LocalWorkspace(workspace.root / "fresh", {
            "sourceCatalog": workspace.roots["sourceCatalog"], "sourceContent": workspace.roots["sourceContent"],
        }) if fresh else workspace
        with prepare_local_experiment(source, selected_workspace, **(settings | changes), processors=processors) as prepared:
            entries = tuple(
                entry for task in prepared.task_source(prepared.handoff)
                for entry in prepared._composition.stores.load(task.input_store).entries
            )
            if on_prepared is not None:
                on_prepared(prepared)
            result = prepared.retain(prepared.run())
            view = open_local_inspection(prepared.plan, selected_workspace,
                document_release_producer=settings["document_release_producer"], release_ref=result)
            return result, view, entries

    return run, retry, fetches, extractor, segmenter
