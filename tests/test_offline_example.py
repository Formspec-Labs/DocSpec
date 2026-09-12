"""Run the documented reference experiment with network access forbidden."""

import json
import runpy
import socket
import sys
from collections import Counter

import pytest

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.processing import ParagraphSegmenter, TextExtractor
from examples.phrase_match_processor import PhraseMatchProcessor


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
    observe(PhraseMatchProcessor, "process", invocations)
    output = tmp_path / "example"
    monkeypatch.setattr(sys, "argv", ["examples.offline_demo", "--output", str(output)])
    with pytest.raises(SystemExit) as completed:
        runpy.run_module("examples.offline_demo", run_name="__main__")
    assert completed.value.code == 0
    summary = json.loads(capfd.readouterr().out)
    assert summary["verdict"] == "pass"
    assert summary["catalogItems"] == 4 and summary["excludedDocuments"] == 1
    assert summary["initialFailures"] == 1 and summary["repairedFailures"] == 0
    assert summary["matchCounts"] == {"original": 4, "case-sensitive": 3, "resource-v2": 5}
    assert summary["processedSegments"] == 6
    assert summary["savedHandoffRecovered"] and summary["cleanOutputValuesAgree"]
    # Two initial captures + one absent-file refusal, one repair acquisition,
    # then three fresh comparison acquisitions. No attempt touches the exclusion.
    assert Counter(candidate.locator for candidate in fetches) == {
        "privacy.txt": 2, "security.txt": 2, "late-arrival.txt": 3,
    }
    assert len(extractions) == len(segmentations) == 6
    assert len(invocations) == 24  # six segments, original + two alternatives + clean run
    original = json.loads((output / "processed.json").read_bytes())
    resource = json.loads((output / "resource-v2.json").read_bytes())
    assert original["plan"]["planId"] != resource["plan"]["planId"]
    assert original["release"] != resource["release"]
    assert resource["inspection"]["work"]["counts"]["newSegments"] == 0
    preview = json.loads((output / "catalog-preview.json").read_bytes())
    assert sum(not item["selectedInRun"] for item in preview["items"]) == 1
    comparisons = json.loads((output / "comparisons.json").read_bytes())
    assert all(value["configurationChanges"] and value["result"]["changeCount"] for value in comparisons.values())
    saved = (output / "experiment-summary.json").read_bytes()
    with pytest.raises(SystemExit) as repeated:
        runpy.run_module("examples.offline_demo", run_name="__main__")
    assert repeated.value.code == 2
    assert (output / "experiment-summary.json").read_bytes() == saved
