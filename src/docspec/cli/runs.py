"""DocSpec command runs: run commands and bounded active progress."""

from __future__ import annotations

import argparse
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docspec.cli.common import _require_new_output_paths, _write_artifact_and_receipt
from docspec.runtime import prepare_local_run
from docspec.cli.requests import (
    _absolute_request_path, _local_run_arguments, _local_run_request, _local_storage_for_run_request,
)
from docspec.cli_io import (
    CliError,
)
from docspec.cli_io import (
    emit as _emit,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.execution import (
    MAX_RESULT_BYTES,
    StoreTask,
    StoreTaskResult,
)
from docspec.domain.identity import (
    canonical_json_file_bytes,
)
from docspec.domain.jobs import StoreState, StoreVerdict
from docspec.domain.references import ArtifactRef


def _cmd_local_run_prepare(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    request = _local_run_request(args.request)
    prepared = prepare_local_run(**_local_run_arguments(request), resume=False)
    receipt = _write_artifact_and_receipt(
        operation="run.prepare",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=prepared.handoff_ref.artifact_id,
        payload=canonical_json_file_bytes(prepared.handoff_ref.to_dict()),
    )
    _emit(receipt)
    return 0


def _local_task_request(path: Path) -> tuple[Path, ArtifactRef, StoreTask]:
    value = _read_json_object(path, label="local task execution request")
    fields = {"format", "formatVersion", "runRequest", "handoff", "task"}
    if set(value) != fields:
        raise CliError("local task execution request has an invalid closed shape")
    if value["format"] != "docspec-local-task-execution-request" or value["formatVersion"] != "1.0":
        raise CliError("local task execution request has an unknown format")
    return (
        _absolute_request_path(value["runRequest"], label="local run request"),
        ArtifactRef.from_dict(value["handoff"]),
        StoreTask.from_dict(value["task"]),
    )


def _cmd_local_task_execute(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    run_request_path, handoff_ref, task = _local_task_request(args.request)
    with prepare_local_run(
        **_local_run_arguments(_local_run_request(run_request_path)), handoff_ref=handoff_ref,
    ) as prepared:
        result = prepared.execute_task(prepared.handoff, task)
    receipt = _write_artifact_and_receipt(
        operation="task.execute",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=result.result_id,
        payload=result.to_bytes(),
    )
    _emit(receipt)
    return 0


def _local_reconcile_request(path: Path) -> tuple[Path, ArtifactRef, Path]:
    value = _read_json_object(path, label="local run reconcile request")
    fields = {"format", "formatVersion", "runRequest", "handoff", "results"}
    if set(value) != fields:
        raise CliError("local run reconcile request has an invalid closed shape")
    if value["format"] != "docspec-local-run-reconcile-request" or value["formatVersion"] != "1.0":
        raise CliError("local run reconcile request has an unknown format")
    return (
        _absolute_request_path(value["runRequest"], label="local run request"),
        ArtifactRef.from_dict(value["handoff"]),
        _absolute_request_path(value["results"], label="task result stream"),
    )


def _iter_task_result_file(path: Path) -> Iterator[StoreTaskResult]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CliError("task result stream must be a regular, non-symlink file")
    with path.open("rb") as handle:
        while line := handle.readline(MAX_RESULT_BYTES + 2):
            if len(line) > MAX_RESULT_BYTES + 1:
                raise CliError("task result exceeds its serialized line limit")
            try:
                yield StoreTaskResult.from_bytes(line)
            except (TypeError, ValueError) as error:
                raise CliError(f"task result stream contains an invalid result: {error}") from error


def _cmd_local_run_reconcile(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    run_request_path, handoff_ref, results_path = _local_reconcile_request(args.request)
    with prepare_local_run(
        **_local_run_arguments(_local_run_request(run_request_path)), handoff_ref=handoff_ref,
    ) as prepared:
        reference = prepared.reconcile(_iter_task_result_file(results_path))
    receipt = _write_artifact_and_receipt(
        operation="run.reconcile",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.artifact_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _cmd_local_run(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    reference = prepare_local_run(
        **_local_run_arguments(_local_run_request(args.request)), resume=args.operation == "run.resume",
    ).run()
    receipt = _write_artifact_and_receipt(
        operation=args.operation,
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.artifact_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _instant_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _cmd_run_active(args: argparse.Namespace) -> int:
    """Report bounded, honest progress for a run that has not finished.

    `run.status` (below) verifies and summarizes a sealed `RunReceipt` --
    a post-hoc artifact that exists only once `run.reconcile` has produced
    one, and it always describes a *completed* run. This command answers the
    question a still-running or crashed run cannot otherwise answer: what
    remains, what failed, and has progress stopped. It is a read-only
    projection over state the run's own machinery already wrote; it writes
    nothing, seals nothing, and must never become a second run ledger --
    the sealed `RunReceipt` stays the only sealed evidence a run produces.

    It reads exactly what `prepare_local_run` and `RunReconciler` already
    read to resume or finish a run: `has_planned_store_ledger` /
    `planned_store_ledger` / `stream_planned_stores` name the complete,
    exact planned population (`run prepare` seals `expected_task_count` and
    `task_set_digest` against this same ledger, so its `record_count` is
    already the run's total task count -- there is no separate handoff to
    load). For each planned store this then loads only that store's *latest
    saved revision*, the same revision `execute_store` and `deliver_store`
    read and extend as a run proceeds, so a store's current `state`
    (planned/running/sealed) and its entries' checkpointed progress are read
    exactly as they stand right now, however far the run got.

    No domain object carries a wall-clock progress timestamp: every instant
    a run persists (`CapturedFile.acquired_at`, a `DeliveryReceipt`'s
    `completed_at`) is the run request's single fixed `completedAt` value,
    held constant across an entire run so its outputs stay reproducible --
    it cannot say how long a store has gone untouched. The filesystem mtime
    of a store's latest saved revision is the only wall-clock signal this
    repository actually has (`LocalDocumentStoreRepository.latest_with_observed_at`),
    so "last observed progress" and "stalled" are derived from it, read-only,
    never written or sealed anywhere.

    Bounded memory: `stream_planned_stores` yields one `StoreRef` at a time
    (O(1) held per store). For each, `latest_with_observed_at` performs one
    directory listing bounded by *that store's own* revision count (in turn
    bounded by the plan's `WorkLimits.max_entries` and the stages each entry
    passes through, not by the run's total store count) and one JSON read
    bounded by the repository's own `max_revision_bytes`. Every result folds
    into fixed-cardinality counters: store state (3 values), store verdict
    (3 values), entry disposition (6 values), and failure signature (class x
    diagnostic code -- `_failure()` builds every diagnostic code from only
    `{stage, exception type name}`, a small closed vocabulary, never from a
    per-item identifier), plus a `--stalled-sample-limit`-capped sample list.
    Peak additional memory is therefore one `DocumentStore` in flight (itself
    bounded by the plan's own limits) plus those fixed-size counters --
    independent of how many stores the run plans in total, satisfying
    Spec section 10.2's bar against holding every item in one graph.
    """

    _request, plan, _controls, stores, _records, _blobs, _catalog = _local_storage_for_run_request(args.request)
    generated_at = _instant_from_epoch(time.time())
    if not stores.has_planned_store_ledger(plan.plan_id):
        _emit(
            {
                "format": "docspec-run-active-view",
                "formatVersion": "2.0",
                "planId": plan.plan_id,
                "phase": "not-planned",
                "generatedAt": generated_at,
            }
        )
        return 0

    planned_ledger = stores.planned_store_ledger(plan.plan_id)
    store_state_counts: Counter[str] = Counter()
    verdict_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    entry_total = 0
    entry_terminal = 0
    failure_total = 0
    captured_entries = 0
    produced_entries = 0
    captured_bytes = 0
    last_observed_at: float | None = None
    now = time.time()
    stalled_after_seconds = args.stalled_after_seconds
    stalled_sample: list[dict[str, Any]] = []
    stalled_count = 0

    for planned_reference in stores.stream_planned_stores(planned_ledger):
        resolved = stores.latest_with_observed_at(planned_reference.store_id)
        if resolved is None:
            raise CliError(f"planned store {planned_reference.store_id!r} has no saved revision")
        latest_reference, observed_at = resolved
        store = stores.load(latest_reference)
        store_state_counts[store.state.value] += 1
        if store.state is not StoreState.PLANNED and (last_observed_at is None or observed_at > last_observed_at):
            last_observed_at = observed_at
        if store.state is StoreState.RUNNING:
            age_seconds = now - observed_at
            if age_seconds >= stalled_after_seconds:
                stalled_count += 1
                if len(stalled_sample) < args.stalled_sample_limit:
                    stalled_sample.append(
                        {
                            "storeId": store.store_id,
                            "lastObservedAt": _instant_from_epoch(observed_at),
                            "ageSeconds": int(age_seconds),
                        }
                    )
        elif store.state is StoreState.SEALED:
            verdict_counts[store.verdict.value] += 1

        entry_total += len(store.entries)
        for entry in store.entries:
            if entry.terminal:
                entry_terminal += 1
                disposition_counts[entry.disposition.value] += 1
            if entry.captured_files:
                captured_entries += 1
                captured_bytes += sum(item.blob.byte_size for item in entry.captured_files)
            if entry.derived_records:
                produced_entries += 1
            for failure in entry.failures:
                failure_total += 1
                failure_counts[f"{failure.failure_class.value}:{failure.diagnostic_code}"] += 1

    planned_count = store_state_counts[StoreState.PLANNED.value]
    running_count = store_state_counts[StoreState.RUNNING.value]
    sealed_count = store_state_counts[StoreState.SEALED.value]
    execution_started = last_observed_at is not None

    progress: dict[str, Any] = {
        "executionStarted": execution_started,
        "stalledAfterSeconds": stalled_after_seconds,
        "stalledStoreCount": stalled_count,
        "stalledStoreSample": stalled_sample,
        "stalledSampleTruncated": stalled_count > len(stalled_sample),
    }
    if last_observed_at is not None:
        progress["lastObservedProgressAt"] = _instant_from_epoch(last_observed_at)
        progress["secondsSinceLastObservedProgress"] = int(now - last_observed_at)

    _emit(
        {
            "format": "docspec-run-active-view",
            "formatVersion": "2.0",
            "planId": plan.plan_id,
            "phase": "planned",
            "generatedAt": generated_at,
            "stores": {
                "total": planned_ledger.record_count,
                "planned": planned_count,
                "running": running_count,
                "sealed": sealed_count,
                "sealedByVerdict": {
                    "completed": verdict_counts[StoreVerdict.COMPLETED.value],
                    "acceptedFailure": verdict_counts[StoreVerdict.ACCEPTED_FAILURE.value],
                    "rejected": verdict_counts[StoreVerdict.REJECTED.value],
                },
                "remaining": planned_count + running_count,
            },
            "entries": {
                "total": entry_total,
                "terminal": entry_terminal,
                "nonTerminal": entry_total - entry_terminal,
                "byDisposition": dict(sorted(disposition_counts.items())),
                "acquisition": {
                    "capturedEntries": captured_entries,
                    "capturedBytes": captured_bytes,
                },
                "processing": {
                    "producedEntries": produced_entries,
                },
            },
            "failures": {
                "totalRecords": failure_total,
                "byClassAndDiagnosticCode": dict(sorted(failure_counts.items())),
            },
            "progress": progress,
            "verificationScope": "planned-store-ledger-and-latest-saved-revisions",
        }
    )
    return 0
