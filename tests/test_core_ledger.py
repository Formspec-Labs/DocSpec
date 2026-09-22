"""Metadata authority and failure boundaries, independent of content publication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
import json
import sqlite3
from threading import Barrier
import time

import pytest

from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.adapters.streams import BATCH_BYTES, BATCH_ROWS
from docspec.domain.core import DependencyEvidence, Execution, Outcome, Result, RetentionPolicy, State
from docspec.domain.identity import sha256_digest
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError, StateTransitionError
from docspec.ports.core_ledger import MetadataBatch, MetadataLink


DIGEST = sha256_digest(b"correspondence")


def state(identity):
    """Build a format-version-1 state record with the given id."""
    return State(format_version=1, state_id=identity)


def result(identity, *, success=True):
    """Build a successful empty or failed result record about the shared `execution`."""
    return Result(
        format_version=1, result_id=identity, execution_id="execution",
        outcome=Outcome(status="success", value="empty") if success else Outcome(status="failed", error="producer failed"),
    )


def records(ledger, *keys):
    """Read the given keys and flatten the batches into one entry per key."""
    return [row for batch in ledger.read_records(keys) for row in batch]


def retained_states(ledger, count=2):
    """Commit `count` states named `s0`, `s1`, ... retained in a single unit."""
    return ledger.commit(MetadataBatch(
        "states", records=tuple(state(f"s{index}") for index in range(count)),
        retained=tuple(("state", f"s{index}") for index in range(count)),
    ))


def test_initialize_reopen_settings_closed_owner_and_namespaced_identity(tmp_path):
    """PRAGMAs are as configured, a closed ledger refuses, and equal ids in different namespaces do not collide."""
    path = tmp_path / "new" / "ledger.sqlite"
    ledger = LocalSqliteCoreLedger(path)
    retained_states(ledger)
    assert ledger.commit(MetadataBatch("same-string-different-kind", records=(result("s0"),)))
    with ledger._transaction() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    ledger.close()
    with pytest.raises(StateTransitionError, match="closed"):
        ledger.current("dataset")
    with closing(LocalSqliteCoreLedger(path, create=False)) as reopened:
        rows = records(reopened, ("state", "s0"), ("result", "s0"), ("state", "missing"))
        assert rows[0].retained and rows[0].available
        assert rows[1].value == result("s0") and not rows[1].retained
        assert rows[2] is None


def test_read_only_snapshot_verification_handles_uri_path_characters(tmp_path):
    path = tmp_path / "export #1?percent%" / "ledger.sqlite"
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        retained_states(ledger)
    before = path.read_bytes()
    with closing(LocalSqliteCoreLedger(path, create=False, read_only=True)) as exported:
        exported.verify_snapshot()
        assert records(exported, ("state", "s0"))[0].value == state("s0")
    assert path.read_bytes() == before


@pytest.mark.parametrize("foreign", [False, True])
def test_unknown_database_or_schema_version_is_refused_without_reinitializing(tmp_path, foreign):
    """A foreign table or a newer `user_version` refuses without modifying the file."""
    path = tmp_path / "ledger.sqlite"
    if foreign:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE user_data (value TEXT)")
            connection.commit()
    else:
        LocalSqliteCoreLedger(path).close()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=99")
            connection.commit()
    before = path.read_bytes()
    with pytest.raises(IntegrityError, match="unsupported"):
        LocalSqliteCoreLedger(path)
    assert path.read_bytes() == before


def test_immutable_record_conflicts_and_failed_unit_rollback(tmp_path):
    """Identical re-commits are no-ops, changed identities refuse, and a failed unit rolls back its receipt."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        original = MetadataBatch("first", records=(result("r"),), retained=(("result", "r"),))
        assert ledger.commit(original)
        assert not ledger.commit(original)
        with pytest.raises(IntegrityError, match="update identity"):
            ledger.commit(replace(original, records=(result("different"),)))
        with pytest.raises(IntegrityError, match="immutable identity"):
            ledger.commit(MetadataBatch("conflict", records=(result("r", success=False),)))
        incoming = MetadataBatch("atomic", records=(state("new"),), retained=(("state", "new"),), links=(
            MetadataLink(("state", "new"), "missing", "input", ("entity", "missing")),
        ))
        with pytest.raises(IntegrityError):
            ledger.commit(incoming)
        assert records(ledger, ("state", "new")) == [None]
        # Rollback includes the unit receipt, so a corrected unit can use that ID.
        assert ledger.commit(replace(incoming, links=()))


def test_retry_after_uncertain_commit_resolves_the_original_unit(tmp_path):
    """A retry after a lost commit response resolves to the already-committed unit instead of duplicating it."""
    class Uncertain(LocalSqliteCoreLedger):
        fail = True

        @contextmanager
        def _transaction(self, *, write=False):
            with super()._transaction(write=write) as connection:
                yield connection
            if write and self.fail:
                self.fail = False
                raise TimeoutError("commit response lost")

    with closing(Uncertain(tmp_path / "ledger.sqlite")) as ledger:
        batch = MetadataBatch("stable", records=(state("s"),), retained=(("state", "s"),))
        with pytest.raises(TimeoutError):
            ledger.commit(batch)
        assert not ledger.commit(batch)
        assert records(ledger, ("state", "s"))[0].available


def test_failure_progress_and_dependency_evidence_survive_without_rewriting_outcomes(tmp_path):
    path = tmp_path / "ledger.sqlite"
    execution = Execution(format_version=1, execution_id="execution", request_id="request")
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        ledger.commit(MetadataBatch("outcomes", records=(execution, result("good"), result("bad", success=False)), retained=(("result", "good"),)))
        assert ledger.record_progress("failure", "execution", "failed", {"message": "interrupted input", "completed_batches": 2})
        assert not ledger.record_progress("failure", "execution", "failed", {"message": "interrupted input", "completed_batches": 2})
        with pytest.raises(IntegrityError, match="successful retention"):
            ledger.record_progress("fake-success", "execution", "success", {})
        with pytest.raises(IntegrityError, match="unsuccessful"):
            ledger.commit(MetadataBatch("retain-bad", retained=(("result", "bad"),)))
        evidence = DependencyEvidence(format_version=1, evidence_id="e", result_id="good", status="omission", description={"missing": "configuration"})
        ledger.commit(MetadataBatch("evidence", records=(evidence, evidence)))
        ledger.commit(MetadataBatch("same-evidence-new-unit", records=(evidence,)))
    with closing(LocalSqliteCoreLedger(path, create=False)) as ledger:
        good, bad = records(ledger, ("result", "good"), ("result", "bad"))
        assert good.value == result("good") and good.evidence_version == 1
        assert not bad.retained and not bad.available
        assert json.loads(next(ledger.read_progress("execution"))[0])["description"]["completed_batches"] == 2
        links = [link for batch in ledger.read_dependencies(["good"]) for link in batch]
        assert links == [MetadataLink(("result", "good"), "evidence", "e", ("dependency_evidence", "e"))]
        with pytest.raises(StaleBaseError):
            ledger.commit(MetadataBatch("stale-evidence", expected_versions=((("result", "good"), 0),)))
        assert ledger.commit(MetadataBatch("fresh-evidence", expected_versions=((("result", "good"), 1),)))


def test_candidate_join_keeps_all_results_duplicate_requests_and_filters_removal(tmp_path):
    """One digest queried twice returns both results twice, and removal hides availability without dropping retention."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        policy = RetentionPolicy(format_version=1, policy_id="policy", description={"allow": "test"})
        ledger.commit(MetadataBatch("results", records=(result("a"), result("b"), policy),
                                    retained=(("result", "a"), ("result", "b")), candidates=((DIGEST, "b"), (DIGEST, "a"))))
        matches = [row for batch in ledger.find_candidates([("q", DIGEST), ("q", DIGEST)]) for row in batch]
        assert [row.result_id for row in matches] == ["a", "b", "a", "b"]
        assert ledger.begin_removal("remove", "policy", [("result", "a")])
        assert not ledger.begin_removal("remove", "policy", [("result", "a")])
        assert records(ledger, ("result", "a"))[0].retained
        assert not records(ledger, ("result", "a"))[0].available
        assert [row.result_id for batch in ledger.find_candidates([("q", DIGEST)]) for row in batch] == ["b"]
        ledger.finish_removal("remove")
        ledger.finish_removal("remove")
        with pytest.raises(IntegrityError, match="policy intent"):
            ledger.finish_removal("unknown")


def test_affected_results_follow_only_declared_data_dependencies_and_output_edges(tmp_path):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        ledger.commit(MetadataBatch("graph", records=(
            *(state(name) for name in ("input", "middle", "end", "other")),
            *(result(name) for name in ("first", "second", "unrelated")),
        ), links=(
            MetadataLink(("result", "first"), "dependency", "input", ("state", "input")),
            MetadataLink(("result", "first"), "requires", "output:data", ("state", "middle")),
            MetadataLink(("result", "second"), "dependency", "input", ("state", "middle")),
            MetadataLink(("result", "second"), "requires", "output:data", ("state", "end")),
            # Conservative declarations may cycle without asserting PROV cycles.
            MetadataLink(("result", "first"), "dependency", "conservative", ("state", "end")),
            MetadataLink(("result", "unrelated"), "dependency", "input", ("state", "other")),
            MetadataLink(("result", "first"), "describes", "context", ("state", "other")),
            MetadataLink(("result", "unrelated"), "requires", "input:context", ("state", "middle")),
        )))
        def affected(*keys):
            return [identity for batch in ledger.affected_results(keys) for identity in batch]
        assert affected(("state", "input")) == ["first", "second"]
        assert affected(("result", "second")) == ["first", "second"]
        assert affected(("state", "other")) == ["unrelated"]
        assert affected(("state", "missing")) == []
        assert affected() == []
        assert affected(*([("state", "input")] * (BATCH_ROWS + 1))) == ["first", "second"]


def test_affected_result_output_is_bounded_and_cancel_releases_the_snapshot(tmp_path):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        ledger.commit(MetadataBatch("input", records=(state("input"),)))
        for offset in range(0, BATCH_ROWS + 1, 512):
            identities = [f"result-{index:05}" for index in range(offset, min(offset + 512, BATCH_ROWS + 1))]
            ledger.commit(MetadataBatch(f"results-{offset}", records=tuple(result(identity) for identity in identities),
                links=tuple(MetadataLink(("result", identity), "dependency", "input", ("state", "input")) for identity in identities)))
        with closing(ledger.affected_results([("state", "input")])) as partial:
            assert len(next(partial)) == BATCH_ROWS
        batches = list(ledger.affected_results([("state", "input")]))
        assert [len(batch) for batch in batches] == [BATCH_ROWS, 1]
        assert ledger.commit(MetadataBatch("after-cancel", records=(state("later"),)))


def test_current_selection_is_guarded_and_has_stable_retry_identity(tmp_path):
    """Selection compare-and-swaps the expected base and a retried unit id stays idempotent."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        retained_states(ledger)
        assert ledger.select_current("first", "dataset", ("state", "s0"), None)
        assert not ledger.select_current("first", "dataset", ("state", "s0"), None)
        with pytest.raises(StaleBaseError):
            ledger.select_current("second", "dataset", ("state", "s1"), None)
        assert ledger.select_current("second", "dataset", ("state", "s1"), ("state", "s0"))
        assert ledger.current("dataset") == ("state", "s1")
        # A retry reconciles historical success even after a subsequent selection.
        assert not ledger.select_current("first", "dataset", ("state", "s0"), None)
        assert ledger.current("dataset") == ("state", "s1")


def test_concurrent_writers_and_current_cas_have_one_winner(tmp_path):
    """Two racing compare-and-swap selections produce exactly one current winner."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        barrier = Barrier(2)

        def write(index):
            target = ("state", f"s{index}")
            ledger.commit(MetadataBatch(f"state-{index}", records=(state(target[1]),), retained=(target,)))
            barrier.wait(timeout=5)
            try:
                ledger.select_current(f"select-{index}", "dataset", target, None)
                return target
            except StaleBaseError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [result for result in pool.map(write, range(2)) if result is not None]
        assert winners == [ledger.current("dataset")]
        assert all(row.retained for row in records(ledger, ("state", "s0"), ("state", "s1")))


def test_busy_wait_is_bounded_and_caller_can_retry(tmp_path):
    """A locked database gives up within the busy timeout, and the caller can retry successfully."""
    path = tmp_path / "ledger.sqlite"
    with closing(LocalSqliteCoreLedger(path, busy_timeout_ms=20)) as ledger:
        batch = MetadataBatch("busy", records=(state("s"),))
        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with pytest.raises(StateTransitionError, match="busy"):
                ledger.commit(batch)
            assert time.monotonic() - started < 1
            blocker.rollback()
        assert ledger.commit(batch)


def test_large_reads_are_set_based_bounded_and_hold_one_snapshot(tmp_path):
    """A large key read issues one set-based statement under one snapshot and streams bounded batches."""
    class Traced(LocalSqliteCoreLedger):
        statements = None

        def _open(self, **kwargs):
            connection = super()._open(**kwargs)
            if self.statements is not None:
                connection.set_trace_callback(self.statements.append)
            return connection

    with closing(Traced(tmp_path / "ledger.sqlite")) as ledger:
        retained_states(ledger, BATCH_ROWS)
        ledger.statements = []
        wanted = [("state", f"s{index % BATCH_ROWS}") for index in range(BATCH_ROWS * 2)] + [("state", "new")]
        with closing(ledger.read_records(wanted)) as reader:
            first = next(reader)
            assert len(first) == BATCH_ROWS
            ledger.commit(MetadataBatch("concurrent", records=(state("new"),)))
            remaining = list(reader)
        assert [len(batch) for batch in remaining] == [BATCH_ROWS, 1]
        rows = [row for batch in [first, *remaining] for row in batch]
        assert [row.key for row in rows[:-1]] == wanted[:-1]
        assert rows[-1] is None
        assert sum(statement.startswith("SELECT w.kind") for statement in ledger.statements) == 1
        assert records(ledger, ("state", "new"))[0] is not None


def test_metadata_read_bytes_are_bounded_independently_of_row_count(tmp_path):
    value = "x" * (BATCH_BYTES // 2 + 1)
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        for index in range(2):
            policy = RetentionPolicy(format_version=1, policy_id=str(index), description={"value": value})
            ledger.commit(MetadataBatch(str(index), records=(policy,)))
        batches = list(ledger.read_records([("retention_policy", "0"), ("retention_policy", "1")]))
        assert [len(batch) for batch in batches] == [1, 1]
        assert all(batch[0].value.description["value"] == value for batch in batches)


def test_removal_intent_can_be_recovered_after_reopen(tmp_path):
    """Pending removal intents survive reopen, keep rows retained but unavailable, and finish cleanly."""
    path = tmp_path / "ledger.sqlite"
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        retained_states(ledger)
        ledger.commit(MetadataBatch("policy", records=(RetentionPolicy(format_version=1, policy_id="p", description={}),)))
        ledger.begin_removal("interrupted", "p", [("state", "s0"), ("state", "s1")])
    with closing(LocalSqliteCoreLedger(path, create=False)) as ledger:
        assert list(ledger.pending_removals()) == [(
            ("interrupted", "p", ("state", "s0")), ("interrupted", "p", ("state", "s1")),
        )]
        assert all(not row.available and row.retained for row in records(ledger, ("state", "s0"), ("state", "s1")))
        ledger.finish_removal("interrupted")
        assert list(ledger.pending_removals()) == []


def test_limits_and_producer_failures_leave_no_partial_units(tmp_path):
    """Too many rows or too many bytes close the producer and leave no partial unit or receipt behind."""
    closed = []

    def source():
        try:
            for index in range(BATCH_ROWS + 10):
                yield state(str(index))
        finally:
            closed.append(True)

    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        with pytest.raises(LimitExceededError, match="2048"):
            ledger.commit(MetadataBatch("large", records=source()))
        assert closed == [True]
        assert records(ledger, ("state", "0")) == [None]
        large = RetentionPolicy(format_version=1, policy_id="large", description={"data": "x" * BATCH_BYTES})
        with pytest.raises(LimitExceededError, match="8 MiB"):
            ledger.commit(MetadataBatch("large", records=(large,)))
        assert ledger.commit(MetadataBatch("large", records=(state("small"),)))
