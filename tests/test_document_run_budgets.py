"""Actual work survives fresh-process continuation; unknown hard-kill work refuses."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from docspec.application.document_run import run_request_id
from docspec.domain.identity import decode_canonical_json_value
from docspec.runtime import CoreWorkspace


@pytest.fixture
def inputs(tmp_path):
    (tmp_path / "inputs").mkdir()
    for name in ("a", "b"):
        (tmp_path / "inputs" / (name + ".txt")).write_bytes(b"12345")
    return tmp_path


def run(root, kind, mode, limit, run_id="run"):
    result = subprocess.run([sys.executable, "-m", "tests.support.document_budget_probe", str(root), kind, mode, run_id, str(limit)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45)
    if mode == "kill":
        assert result.returncode == 23, result.stderr
        return
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def accounting(root, run_id="run"):
    with CoreWorkspace(root / "workspace") as workspace:
        execution, = [identity for batch in workspace.ledger.executions(run_request_id(run_id)) for identity in batch]
        result = next(workspace.ledger.read_records([("result", execution + ":result")]))[0]
        if result is not None and result.retained:
            work_id = next(binding.entity_id for binding in result.value.outcome.outputs if binding.label == "work")
            return execution, next(workspace.ledger.read_records([("entity", work_id)]))[0].value.value.value
        progress = [decode_canonical_json_value(payload, label="test progress")
                    for batch in workspace.ledger.read_progress(execution) for payload in batch]
        with workspace.publisher.session() as session:
            return execution, session.read_json(progress[-1]["description"]["checkpoint"])["state"]


@pytest.mark.parametrize("limit,success", [(12, False), (13, True)])
def test_source_budget_survives_failure_reopen_and_does_not_charge_reused_capture(inputs, limit, success):
    assert run(inputs, "bytes", "fail", limit)["error"] == "RuntimeError"
    execution, counts = accounting(inputs)
    assert counts["source_bytes"] == 8 and counts["generated_rows"] == 0
    restored = run(inputs, "bytes", "resume", limit)
    assert ("state" in restored) is success
    if not success:
        assert restored["error"] == "LimitExceededError"
    continued_execution, counts = accounting(inputs)
    assert continued_execution == execution
    assert counts["source_bytes"] == 13
    assert (inputs / "fetches.txt").read_text().splitlines() == ["a", "b", "b"]
    if success:
        assert counts["generated_rows"] == 4
        assert run(inputs, "bytes", "resume", limit) == {"state": "run"}
        assert accounting(inputs)[1] == counts
        assert (inputs / "fetches.txt").read_text().splitlines() == ["a", "b", "b"]


def test_failed_processor_rows_remain_charged_after_reopening(inputs):
    run(inputs, "rows", "seed", 3)
    assert run(inputs, "rows", "fail", 3)["error"] == "RuntimeError"
    execution, counts = accounting(inputs)
    assert counts["generated_rows"] == 2 and counts["source_bytes"] == 0
    assert run(inputs, "rows", "resume", 3)["error"] == "LimitExceededError"
    assert accounting(inputs)[0] == execution
    assert accounting(inputs)[1]["generated_rows"] == 4
    assert (inputs / "fetches.txt").read_text().splitlines() == ["a"]


def test_zero_output_attempt_is_recorded_once_and_reuse_costs_no_new_rows(inputs):
    run(inputs, "empty", "seed", 0)
    assert run(inputs, "empty", "resume", 0) == {"state": "run"}
    assert accounting(inputs)[1] == {"source_bytes": 0, "generated_rows": 0, "expected_current": None}
    assert run(inputs, "empty", "resume", 0) == {"state": "run"}
    assert run(inputs, "empty", "resume", 0, run_id="another") == {"state": "another"}
    assert (inputs / "processors.txt").read_text().splitlines() == ["resume"]
    with CoreWorkspace(inputs / "workspace") as workspace:
        with workspace.ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='result' AND json_extract(payload,'$.outcome.value')='empty'").fetchone() == (1,)


def test_limits_are_pinned_before_any_resumed_work(inputs):
    run(inputs, "bytes", "fail", 13)
    assert run(inputs, "bytes", "resume", 14)["error"] == "IntegrityError"
    assert (inputs / "fetches.txt").read_text().splitlines() == ["a", "b"]
    assert accounting(inputs)[1]["source_bytes"] == 8


def test_hard_kill_refuses_unknown_consumption_instead_of_resetting_the_budget(inputs):
    run(inputs, "bytes", "kill", 13)
    assert run(inputs, "bytes", "resume", 13)["error"] == "StateTransitionError"
    assert (inputs / "fetches.txt").read_text().splitlines() == ["a", "b"]
    assert run(inputs, "bytes", "resume", 13, run_id="explicit-new-budget") == {"state": "explicit-new-budget"}


def test_concurrent_initial_run_claim_allows_only_one_producer(inputs, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from docspec.adapters.content_fetchers import LocalFileContentFetcher
    from docspec.application.core_execution import CoreOperations
    from docspec.domain.content import CandidateFile, SourceItem
    from docspec.errors import IntegrityError

    with CoreWorkspace(inputs / "workspace") as workspace:
        workspace.documents(fetcher=LocalFileContentFetcher(inputs / "inputs")).import_sources(
            [SourceItem("a", "1", (CandidateFile("a", "a.txt", "text/plain"),))], state_id="source")
    barrier = Barrier(2)
    prepare = CoreOperations.prepare

    def simultaneous(self, definition, *args, **kwargs):
        if definition.implementation_id == "docspec.document-run":
            barrier.wait(timeout=10)
        return prepare(self, definition, *args, **kwargs)

    monkeypatch.setattr(CoreOperations, "prepare", simultaneous)

    def invoke():
        with CoreWorkspace(inputs / "workspace") as workspace:
            try:
                return workspace.documents(fetcher=LocalFileContentFetcher(inputs / "inputs")).run(
                    "source", run_id="run", max_source_bytes=5, max_generated_rows=2).state_id
            except IntegrityError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: invoke(), range(2))) == ["conflict", "run"]
    assert accounting(inputs)[1]["source_bytes"] == 5
