"""Task admission happens before work and retains no second task authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from docspec.adapters.storage import LocalDocumentStoreRepository
from docspec.application.commit import ReleaseCommitService
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.execution import StoreTask, summarize_store_tasks
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import DocumentStore
from docspec.domain.plans import ProcessingPlan
from docspec.errors import IntegrityError, LimitExceededError
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run
from docspec.runtime.task_membership import _TaskMembershipIndex
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


@pytest.fixture
def prepared(tmp_path: Path):
    request, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(request))
    fetcher = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    with prepare_local_run(**arguments, content_fetcher=fetcher) as run:
        yield run


def _tasks(prepared):
    return tuple(prepared.task_source(prepared.handoff))


def _scratch(prepared) -> Path:
    return prepared._composition.workspace.roots["reconciliation"] / "task-membership"


def _assert_no_index(directory: Path) -> None:
    assert not directory.exists() or list(directory.iterdir()) == []


@pytest.mark.parametrize("change", ["extra_store", "later_revision", "locator", "digest"])
def test_task_outside_exact_population_refuses_before_recovery_or_side_effects(prepared, monkeypatch, change) -> None:
    task = _tasks(prepared)[0]
    stores = prepared._composition.stores
    original = stores.load(task.input_store)
    if change == "extra_store":
        reference = stores.save(DocumentStore.planned(
            plan_id=original.plan_id, logical_partition="unplanned-partition",
            entries=original.entries, limits=original.limits,
        ))
    elif change == "later_revision":
        reference = stores.save(original.start("unexpected-task-input-attempt"))
    elif change == "locator":
        reference = replace(task.input_store, locator="another/initial-store.json")
    else:
        reference = replace(task.input_store, digest=sha256_digest(b"different-store"))
    invalid = replace(task, input_store=reference)
    before = {path: path.read_bytes() for path in stores.root.rglob("*") if path.is_file()}

    def unexpected(*args, **kwargs):
        raise AssertionError("task admission must precede recovery, execution, fetching, and delivery")

    monkeypatch.setattr("docspec.runtime.execution.load_latest_store", unexpected)
    monkeypatch.setattr(prepared._composition.executor, "execute_store", unexpected)
    monkeypatch.setattr(prepared._composition.delivery, "deliver_store", unexpected)
    monkeypatch.setattr(prepared._composition.content_fetcher, "fetch", unexpected)
    with pytest.raises(IntegrityError, match="initial planned-store|outside the sealed planned-store"):
        prepared.execute_task(prepared.handoff, invalid)
    assert {path: path.read_bytes() for path in stores.root.rglob("*") if path.is_file()} == before
    prepared.close()
    _assert_no_index(_scratch(prepared))


def test_initial_member_resumes_latest_revision_without_fetching_again(prepared, monkeypatch) -> None:
    task = _tasks(prepared)[0]
    first = prepared.execute_task(prepared.handoff, task)
    assert first.output_store.revision > task.input_store.revision
    prepared.close()
    _assert_no_index(_scratch(prepared))

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("verified completed work must not fetch again")

    monkeypatch.setattr(prepared._composition.content_fetcher, "fetch", unexpected_fetch)
    assert prepared.execute_task(prepared.handoff, task) == first
    assert prepared.run() == prepared.reconcile((first,))
    _assert_no_index(_scratch(prepared))


def test_concurrent_lookups_build_once_and_close_allows_verified_rebuild(prepared, monkeypatch) -> None:
    task = _tasks(prepared)[0]
    stores = prepared._composition.stores
    stream = stores.stream_planned_stores
    builds = []

    def observed(reference):
        builds.append(reference)
        yield from stream(reference)

    monkeypatch.setattr(stores, "stream_planned_stores", observed)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(prepared._membership.require_member, [task.input_store] * 40))
    assert builds == [prepared.handoff.planned_store_ledger]
    prepared.close()
    prepared.close()
    _assert_no_index(_scratch(prepared))
    prepared._membership.require_member(task.input_store)
    assert builds == [prepared.handoff.planned_store_ledger] * 2


def test_incomplete_or_mismatched_population_never_becomes_ready(prepared, monkeypatch, tmp_path) -> None:
    task = _tasks(prepared)[0]
    stores = prepared._composition.stores
    stream = stores.stream_planned_stores
    closed = []

    def observed(reference):
        try:
            yield from stream(reference)
        finally:
            closed.append(True)

    monkeypatch.setattr(stores, "stream_planned_stores", observed)
    handoff = replace(prepared.handoff, task_set_digest=sha256_digest(b"another-task-set"))
    index = _TaskMembershipIndex(stores, handoff, tmp_path / "mismatched-index", max_scratch_bytes=1024**2)
    for _ in range(2):
        with pytest.raises(IntegrityError, match="differs from the sealed execution handoff"):
            index.require_member(task.input_store)
        _assert_no_index(tmp_path / "mismatched-index")
    assert closed == [True, True]


def test_stream_failure_closes_partial_index_and_iterator(prepared, monkeypatch, tmp_path) -> None:
    task = _tasks(prepared)[0]
    closed = []

    def interrupted(reference):
        try:
            yield task.input_store
            raise IntegrityError("interrupted planned store stream")
        finally:
            closed.append(True)

    monkeypatch.setattr(prepared._composition.stores, "stream_planned_stores", interrupted)
    index = _TaskMembershipIndex(
        prepared._composition.stores, prepared.handoff, tmp_path / "interrupted-index", max_scratch_bytes=1024**2,
    )
    with pytest.raises(IntegrityError, match="interrupted planned store stream"):
        index.require_member(task.input_store)
    assert closed == [True]
    _assert_no_index(tmp_path / "interrupted-index")


def test_sqlite_page_limit_refuses_and_removes_partial_database(prepared, tmp_path) -> None:
    original = prepared._composition.stores.load(_tasks(prepared)[0].input_store)
    stores = LocalDocumentStoreRepository(tmp_path / "many-stores")
    references = tuple(stores.save(DocumentStore.planned(
        plan_id=original.plan_id, logical_partition=f"member-{ordinal}",
        entries=original.entries, limits=original.limits,
    )) for ordinal in range(64))
    ledger = stores.seal_planned_stores(original.plan_id, references)
    count, digest = summarize_store_tasks(
        StoreTask(original.plan_id, prepared.handoff.operation_id, reference) for reference in references
    )
    handoff = replace(prepared.handoff, planned_store_ledger=ledger, expected_task_count=count, task_set_digest=digest)
    index = _TaskMembershipIndex(stores, handoff, tmp_path / "bounded-index", max_scratch_bytes=8192)
    with pytest.raises(LimitExceededError, match="exceeds its scratch allowance"):
        index.require_member(references[0])
    _assert_no_index(tmp_path / "bounded-index")


def test_worker_scratch_limit_is_applied_before_any_execution(prepared, monkeypatch) -> None:
    bounded = replace(prepared, execution_profile=replace(prepared.execution_profile, max_task_index_bytes=4096))
    task = _tasks(bounded)[0]

    def unexpected(*args, **kwargs):
        raise AssertionError("scratch refusal must precede store recovery")

    monkeypatch.setattr("docspec.runtime.execution.load_latest_store", unexpected)
    with pytest.raises(LimitExceededError, match="scratch allowance is too small"):
        bounded.execute_task(bounded.handoff, task)
    _assert_no_index(_scratch(bounded))


def test_deadline_expiring_during_admission_refuses_before_recovery(prepared, monkeypatch) -> None:
    task = _tasks(prepared)[0]
    deadline = prepared.execution_profile.deadline_epoch_seconds
    times = iter((deadline - 1, deadline))
    monkeypatch.setattr("docspec.runtime.execution.time", SimpleNamespace(time=lambda: next(times)))

    def unexpected(*args, **kwargs):
        raise AssertionError("expired admission must not start store recovery")

    monkeypatch.setattr("docspec.runtime.execution.load_latest_store", unexpected)
    with pytest.raises(LimitExceededError, match="deadline has expired"):
        prepared.execute_task(prepared.handoff, task)


def test_zero_task_successor_creates_no_membership_index(tmp_path) -> None:
    request, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(request))
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    with prepare_local_run(**arguments) as initial:
        run_ref = initial.run()
        composition = initial._composition
        base = ReleaseCommitService(
            plan_ref=composition.plan_ref, controls=composition.controls,
            records=composition.records, document_catalog=composition.catalog,
        ).commit_release(None, run_ref)
    previous = arguments["plan"]
    content = {field.name: getattr(previous, field.name) for field in fields(previous) if field.name != "plan_id"}
    arguments["plan"] = ProcessingPlan.create(**(content | {"base_release": base}))
    with prepare_local_run(**arguments) as empty:
        assert empty.handoff.expected_task_count == 0
        empty.run()
        _assert_no_index(_scratch(empty))


def test_run_failure_and_context_exit_release_index(prepared, monkeypatch) -> None:
    def fail(*args):
        raise RuntimeError("worker failed after task admission")

    monkeypatch.setattr(prepared._composition.executor, "execute_store", fail)
    with pytest.raises(RuntimeError, match="worker failed after task admission"):
        prepared.run()
    _assert_no_index(_scratch(prepared))
    with prepared:
        prepared._membership.require_member(_tasks(prepared)[0].input_store)
        assert list(_scratch(prepared).iterdir())
    _assert_no_index(_scratch(prepared))
