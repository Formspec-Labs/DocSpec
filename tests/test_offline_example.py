"""The public Core walkthrough preserves repair, reuse, quotes and no-network behavior."""

import json
import runpy
import socket
import sys
from collections import Counter

import pytest

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.documents import DocumentPipeline
from docspec.processing import ParagraphSegmenter, TextExtractor
from examples.phrase_match_processor import PhraseMatcher


def test_reference_experiment_repairs_and_compares_without_repeating_upstream_work(tmp_path, monkeypatch, capfd):
    def reject_network(*args, **kwargs):
        pytest.fail("the contributor example attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    fetches, extractions, segmentations, invocations = [], [], [], []
    def observe(cls, method, calls):
        original = getattr(cls, method)
        def counted(self, *args, **kwargs):
            calls.append(args[0])
            return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, method, counted)
    observe(LocalFileContentFetcher, "fetch", fetches)
    observe(TextExtractor, "extract", extractions)
    observe(ParagraphSegmenter, "segment", segmentations)
    observe(PhraseMatcher, "__call__", invocations)
    actual_run = DocumentPipeline.run
    phases = []
    def run(self, *args, **kwargs):
        before = tuple(len(calls) for calls in (fetches, extractions, segmentations, invocations))
        try:
            return actual_run(self, *args, **kwargs)
        finally:
            phases.append(tuple(len(calls) - count for calls, count in zip(
                (fetches, extractions, segmentations, invocations), before, strict=True)))
    monkeypatch.setattr(DocumentPipeline, "run", run)
    output = tmp_path / "example"
    monkeypatch.setattr(sys, "argv", ["examples.offline_demo", "--output", str(output)])
    with pytest.raises(SystemExit) as completed:
        runpy.run_module("examples.offline_demo", run_name="__main__")
    assert completed.value.code == 0
    summary = json.loads(capfd.readouterr().out)
    assert summary["verdict"] == "pass"
    assert summary["initialCatalogItems"] == 4 and summary["catalogItems"] == 5
    assert summary["excludedDocuments"] == summary["addedDocuments"] == 1
    assert summary["initialFailures"] == 1 and summary["repairedFailures"] == 0
    assert summary["matchCounts"] == {"original": 4, "case-sensitive": 3, "resource-v2": 5, "grown": 7}
    assert summary["processedSegments"] == 7
    assert summary["exactSelectionsRecovered"] and summary["cleanOutputValuesAgree"]
    assert Counter(candidate.locator for candidate in fetches) == {
        "privacy.txt": 2, "security.txt": 2, "late-arrival.txt": 3, "added-note.txt": 2}
    assert phases == [(3, 0, 0, 0), (1, 0, 0, 0), (0, 3, 3, 6), (0, 0, 0, 0),
                      (0, 0, 0, 6), (0, 0, 0, 6), (1, 1, 1, 1), (4, 4, 4, 7)]
    preview = json.loads((output / "catalog-preview.json").read_bytes())
    assert sum(not item["selectedInRun"] for item in preview["items"]) == 1
    comparisons = json.loads((output / "comparisons.json").read_bytes())
    assert all(value["counts"]["changed"] for value in comparisons.values())
    saved = (output / "experiment-summary.json").read_bytes()
    with pytest.raises(SystemExit) as repeated:
        runpy.run_module("examples.offline_demo", run_name="__main__")
    assert repeated.value.code == 2
    assert (output / "experiment-summary.json").read_bytes() == saved
