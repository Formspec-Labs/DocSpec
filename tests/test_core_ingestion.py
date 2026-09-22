"""Incremental catalogue imports preserve old states and exact batch retries."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from docspec.domain.identity import stable_urn
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from docspec.runtime import CoreWorkspace
from tests.support.iceberg_records import files


def values(workspace, state_id):
    """Map member keys to their decoded values for one state."""
    return {key: entity.value.value for key, entity in workspace.rows(state_id)}


def test_upsert_adds_replaces_and_reopens_without_rewriting_base(tmp_path):
    """An upsert leaves the base state untouched, adds and replaces rows, and retrying the same batch id is idempotent."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("unchanged", 1), ("update", {"title": "old"})])
        workspace.maintenance.select_current("initial", "regulations", ("state", "base"), None)
        with workspace.publisher.session() as session:
            base_files = files(workspace.records, workspace.states.layers(session, "base")["entities"])
        rows = [("new", None), ("update", {"title": "new"})]
        state = workspace.upsert("base", iter(rows), batch_id="daily", dataset="regulations")
        assert workspace.ledger.current("regulations") == ("state", state.state_id)
        assert values(workspace, "base") == {"unchanged": 1, "update": {"title": "old"}}
        assert values(workspace, state.state_id) == {"unchanged": 1, "update": {"title": "new"}, "new": None}
        with workspace.publisher.session() as session:
            new_files = files(workspace.records, workspace.states.layers(session, state.state_id)["entities"])
            assert all(item in new_files for item in base_files)
    with CoreWorkspace(tmp_path) as workspace:
        assert workspace.upsert("base", rows, batch_id="daily", dataset="regulations") == state
        request_id = stable_urn("core-upsert", "daily") + ":request"
        assert len([item for group in workspace.ledger.executions(request_id) for item in group]) == 1
        # A later observation with equal content still has distinct provenance.
        observed = workspace.upsert(state.state_id, rows, batch_id="tomorrow", dataset="regulations")
        assert workspace.compare(state.state_id, observed.state_id)["counts"] == {"added": 0, "changed": 2, "removed": 0}
        assert workspace.upsert("base", rows, batch_id="daily", dataset="regulations") == state
        assert workspace.ledger.current("regulations") == ("state", observed.state_id)


@pytest.mark.parametrize("change", ["value", "base", "dataset", "order"])
def test_batch_identity_refuses_different_input(tmp_path, change):
    """Reusing a batch id with different values, base, dataset or row order refuses."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        rows = [("a", 1), ("b", "1")]
        state = workspace.upsert("base", rows, batch_id="batch")
        base, dataset = "base", None
        if change == "value":
            rows[0] = ("a", "1")
        elif change == "base":
            base = state.state_id
        elif change == "dataset":
            dataset = "data"
        else:
            rows.reverse()
        with pytest.raises(IntegrityError, match="batch ID"):
            workspace.upsert(base, rows, batch_id="batch", dataset=dataset)


@pytest.mark.parametrize("rows", [[], [("a", 1), ("a", 2)], [(1, "bad-key")], [("bad", 1.5)]])
def test_invalid_input_closes_stream_and_does_not_start_attempt(tmp_path, rows):
    """Empty, duplicate-key, non-string-key or non-JSON rows close the source and start no attempt."""
    closed = []
    def source():
        try:
            yield from rows
        finally:
            closed.append(True)
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [])
        with pytest.raises((IntegrityError, ValueError)):
            workspace.upsert("base", source(), batch_id="bad")
        assert closed == [True]
        assert not list(workspace.ledger.executions(stable_urn("core-upsert", "bad") + ":request"))


def test_upsert_streams_more_than_one_metadata_batch(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        state = workspace.upsert("base", ((str(i), "x" * 4096) for i in range(2049)), batch_id="bulk")
        assert workspace.compare("base", state.state_id, sample_limit=0)["counts"] == {"added": 2049, "changed": 0, "removed": 0}


@pytest.mark.parametrize("boundary", ["producer", "before_journal", "publication", "promotion"])
def test_retry_recovers_after_interruption(tmp_path, monkeypatch, boundary):
    """Retrying the same batch resumes from whatever durable prefix the interrupted boundary left."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        workspace.maintenance.select_current("initial", "data", ("state", "base"), None)
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError("simulated interruption")
            if boundary == "producer":
                # Input state has been retained; composition has no journal yet.
                patch.setattr(workspace.states, "revision_representation", fail)
            elif boundary == "before_journal":
                patch.setattr(workspace.operations, "publish", fail)
            elif boundary == "publication":
                patch.setattr(workspace.operations, "_publish_journal", fail)
            else:
                patch.setattr(workspace.ledger, "select_current", fail)
            with pytest.raises(OSError, match="simulated"):
                workspace.upsert("base", [("new", 2)], batch_id="batch", dataset="data")
        assert workspace.ledger.current("data") == ("state", "base")
    with CoreWorkspace(tmp_path) as workspace:
        state = workspace.upsert("base", [("new", 2)], batch_id="batch", dataset="data")
        assert values(workspace, state.state_id) == {"old": 1, "new": 2}
        assert workspace.ledger.current("data") == ("state", state.state_id)
        request_id = stable_urn("core-upsert", "batch") + ":request"
        assert len([item for group in workspace.ledger.executions(request_id) for item in group]) == (2 if boundary in {"producer", "before_journal"} else 1)


def test_concurrent_head_change_keeps_published_branch_and_retry_does_not_overwrite(tmp_path, monkeypatch):
    """A concurrent head advance makes the upsert stale, and the published branch is recovered without overwriting the head."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        workspace.create("other", [("other", 3)])
        workspace.maintenance.select_current("initial", "data", ("state", "base"), None)
        publish = workspace.operations._publish_journal
        def advance(session, journal, **kwargs):
            publish(session, journal, **kwargs)
            workspace.maintenance.select_current("other", "data", ("state", "other"), ("state", "base"))
        with monkeypatch.context() as patch:
            patch.setattr(workspace.operations, "_publish_journal", advance)
            with pytest.raises(StaleBaseError):
                workspace.upsert("base", [("new", 2)], batch_id="batch", dataset="data")
        with pytest.raises(StaleBaseError):
            workspace.upsert("base", [("new", 2)], batch_id="batch", dataset="data")
        assert workspace.ledger.current("data") == ("state", "other")
        execution = next(workspace.ledger.executions(stable_urn("core-upsert", "batch") + ":request"))[0]
        result = workspace.operations.recover(execution)
        assert values(workspace, result.outcome.outputs[0].entity_id) == {"old": 1, "new": 2}


def test_same_batch_cannot_execute_concurrently(tmp_path):
    """A second attempt at the same batch id while its request guard is held refuses as running."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [])
        request_id = stable_urn("core-upsert", "batch") + ":request"
        with workspace.ledger.request_guard(request_id), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(workspace.upsert, "base", [("new", 2)], batch_id="batch")
            with pytest.raises(StateTransitionError, match="request is running"):
                future.result()
        assert values(workspace, workspace.upsert("base", [("new", 2)], batch_id="batch").state_id) == {"new": 2}
