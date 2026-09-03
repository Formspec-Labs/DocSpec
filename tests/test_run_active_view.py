"""Tests for ``docspec run active`` -- a bounded, read-only progress view.

`run.status` (`_cmd_run_status`) only knows how to summarize a sealed
`RunReceipt`, an artifact that exists solely once `run.reconcile` has already
produced one and always describes a *completed* run. These tests instead
build runs that are genuinely mid-flight -- some stores planned only, one
crashed after claiming its work, some sealed clean, one sealed with an
accepted failure -- and prove `run active` reports the split honestly from
whatever is on disk right now, without requiring the run to finish first.

Fixture-building reuses the patterns already proven in
`tests/conformance/test_incremental_equivalence.py` and
`tests/conformance/test_scheduler_portability.py` (real multi-store runs
through `RunPlanner` and the CLI's local composition) and `tests/test_cli.py`
(the local-run-request and portable-local-profile helpers), rather than
inventing new fixture machinery.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

import docspec.cli as cli_module
from docspec.adapters.storage import LocalDocumentStoreRepository, LocalJsonControlRepository
from docspec.cli import main
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.execution import ExecutionHandoff, StoreTask
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.jobs import FailureClass
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.references import ArtifactRef
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_cli_helpers = importlib.import_module("tests.test_cli")
_helpers = importlib.import_module("tests.helpers")
_portable_local_profiles = _cli_helpers._portable_local_profiles
_write_local_run_request = _cli_helpers._write_local_run_request
write_shared_source_catalog = _helpers.write_shared_source_catalog
SharedFixtureContentFetcher = _helpers.SharedFixtureContentFetcher


def _run_request(
    tmp_path: Path,
    *,
    items: tuple[SourceItem, ...],
    processors: ProcessorSet,
    processor_ids: tuple[str, ...],
    retry,
    accepted,
) -> tuple[Path, dict[str, str], ProcessingPlan]:
    """Seal one local run request over ``items``, one entry per store.

    ``items`` must already be sorted by ``item_id``: the source-native
    catalog format requires strictly ordered records, same as every other
    fixture that calls ``write_shared_source_catalog``.
    """

    source_content = tmp_path / "source-content"
    source_content.mkdir()
    source_catalog_root = tmp_path / "source-catalog"
    source_ref = write_shared_source_catalog(source_catalog_root, items)
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=_portable_local_profiles(),
        limits=WorkLimits(1, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy(
            (DefaultExtractorRegistry.extractor_id,),
            DefaultSegmenterRegistry.segmenter_id,
            processor_ids,
        ),
        processors=processors,
        partition_count=4,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(plan.to_dict()))
    roots = {
        "blobStorage": (tmp_path / "blobs").as_posix(),
        "controlRepository": (tmp_path / "controls").as_posix(),
        "documentCatalog": (tmp_path / "catalog").as_posix(),
        "documentStores": (tmp_path / "stores").as_posix(),
        "reconciliation": (tmp_path / "reconciliation").as_posix(),
        "recordStorage": (tmp_path / "records").as_posix(),
        "sourceCatalog": source_catalog_root.as_posix(),
        "sourceContent": source_content.as_posix(),
    }
    run_request = _write_local_run_request(
        tmp_path / "run-request.json",
        plan_path=plan_path,
        roots=roots,
        result_sink_id="urn:docspec:test:sink:local-durable",
        retry=retry,
        accepted=accepted,
        completed_at="2026-08-05T12:00:00Z",
        max_workers=1,
        max_in_flight=1,
    )
    return run_request, roots, plan


def _prepare(tmp_path: Path, run_request: Path) -> tuple[ArtifactRef, ExecutionHandoff]:
    handoff_reference_path = tmp_path / "handoff-reference.json"
    assert (
        main(
            [
                "run",
                "prepare",
                "--request",
                str(run_request),
                "--destination",
                str(handoff_reference_path),
                "--receipt",
                str(tmp_path / "prepare-operation.json"),
            ]
        )
        == 0
    )
    handoff_reference = ArtifactRef.from_dict(json.loads(handoff_reference_path.read_text()))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    handoff = ExecutionHandoff.from_dict(controls.load(handoff_reference))
    return handoff_reference, handoff


def _execute_task(
    tmp_path: Path,
    *,
    run_request: Path,
    handoff_reference: ArtifactRef,
    handoff: ExecutionHandoff,
    plan: ProcessingPlan,
    store_reference,
    label: str,
) -> None:
    """Drive one planned store to its terminal outcome through the real CLI.

    This is the same ``task execute`` seam a distributed worker uses; the
    only reason a full run doesn't finish this way is that the operator
    chooses not to call it for every planned store.
    """

    task = StoreTask(plan.plan_id, handoff.operation_id, store_reference)
    task_request = tmp_path / f"task-request-{label}.json"
    task_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-task-execution-request",
                "formatVersion": "1.0",
                "runRequest": run_request.as_posix(),
                "handoff": handoff_reference.to_dict(),
                "task": task.to_dict(),
            }
        )
    )
    result_path = tmp_path / f"task-result-{label}.jsonl"
    assert (
        main(
            [
                "task",
                "execute",
                "--request",
                str(task_request),
                "--destination",
                str(result_path),
                "--receipt",
                str(tmp_path / f"task-operation-{label}.json"),
            ]
        )
        == 0
    )


def _run_active(
    run_request: Path,
    capfd: pytest.CaptureFixture[str],
    *extra_args: str,
) -> dict:
    """Invoke ``run active`` and parse its one canonical JSON object.

    Drains any stdout left over from earlier CLI calls in the same test
    first, so this always reads exactly this invocation's output.
    """

    capfd.readouterr()
    assert main(["run", "active", "--request", str(run_request), *extra_args]) == 0
    return json.loads(capfd.readouterr().out)


def test_run_active_reports_absence_before_any_planning(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """No planned-store ledger yet -- say so, don't report zeros that look like progress."""

    item = SourceItem("solo-item", "v1", (), state=SourceItemState.DELETED)
    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0, max_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    run_request, _roots, plan = _run_request(
        tmp_path,
        items=(item,),
        processors=ProcessorSet(()),
        processor_ids=(),
        retry=retry,
        accepted=accepted,
    )
    view = _run_active(run_request, capfd)
    assert view["format"] == "docspec-run-active-view"
    assert view["phase"] == "not-planned"
    assert view["planId"] == plan.plan_id
    # Nothing about stores, entries, failures, or progress is asserted here --
    # it must not exist. A field defaulted to zero would read as "planned and
    # nothing has happened yet", which is a different, false claim.
    assert set(view) == {"format", "formatVersion", "planId", "phase", "generatedAt"}


def test_run_active_reports_planned_but_unexecuted_stores_honestly(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``run prepare`` alone must not manufacture execution progress."""

    item = SourceItem("solo-item", "v1", (), state=SourceItemState.DELETED)
    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0, max_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    run_request, _roots, _plan = _run_request(
        tmp_path,
        items=(item,),
        processors=ProcessorSet(()),
        processor_ids=(),
        retry=retry,
        accepted=accepted,
    )
    _prepare(tmp_path, run_request)
    view = _run_active(run_request, capfd)
    assert view["phase"] == "planned"
    assert view["stores"] == {
        "total": 1,
        "planned": 1,
        "running": 0,
        "sealed": 0,
        "sealedByVerdict": {"completed": 0, "acceptedFailure": 0, "rejected": 0},
        "remaining": 1,
    }
    assert view["progress"]["executionStarted"] is False
    assert "lastObservedProgressAt" not in view["progress"]
    assert "secondsSinceLastObservedProgress" not in view["progress"]
    assert view["progress"]["stalledStoreCount"] == 0


def test_run_active_distinguishes_planned_running_sealed_and_failed_stores(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Plan five stores, execute three to different outcomes, crash one, and
    leave one untouched -- the archetypal mid-flight run this command exists
    to answer questions about.

    - ``item-planned``: never touched. Stays PLANNED.
    - ``item-running``: an ACTIVE item whose store is driven straight to
      RUNNING via ``DocumentStore.start`` and saved, without ever calling
      ``execute_store`` -- exactly what a worker crashing right after
      claiming a task leaves behind: a running store with a non-terminal
      entry no processor has touched yet.
    - ``item-sealed``: a DELETED item executed to completion (COMPLETED).
    - ``item-active-fail``: an ACTIVE item whose only candidate names a file
      that was never written, so acquisition fails with a real
      ``FileNotFoundError``; classified TRANSIENT_EXTERNAL and accepted by
      policy, sealing ACCEPTED_FAILURE with one real failure record.
    - ``item-active-ok``: an ACTIVE item with real text content, captured,
      extracted, segmented, and processed by ``ContentStatisticsProcessor``,
      sealing COMPLETED with non-empty captured files and derived records --
      this is what lets acquisition progress and processor progress be told
      apart, rather than both reading zero.
    """

    source_content = tmp_path / "source-content"
    source_content.mkdir()
    text = "Alpha paragraph one.\n\nAlpha paragraph two."
    (source_content / "a.txt").write_text(text, encoding="utf-8")
    payload = (source_content / "a.txt").read_bytes()

    ok_item = SourceItem(
        "item-active-ok",
        "v1",
        (
            CandidateFile(
                "primary",
                "a.txt",
                "text/plain",
                expected_digest=sha256_digest(payload),
                expected_size=len(payload),
            ),
        ),
        metadata={"expectedSegments": 1},
    )
    fail_item = SourceItem(
        "item-active-fail",
        "v1",
        (CandidateFile("primary", "missing.txt", "text/plain"),),
    )
    running_item = SourceItem(
        "item-running",
        "v1",
        (CandidateFile("primary", "never-fetched.txt", "text/plain"),),
    )
    sealed_item = SourceItem("item-sealed", "v1", (), state=SourceItemState.DELETED)
    planned_item = SourceItem("item-planned", "v1", (), state=SourceItemState.DELETED)
    items = tuple(
        sorted((sealed_item, planned_item, running_item, fail_item, ok_item), key=lambda i: i.item_id)
    )

    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0, max_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy(accepted_classes=(FailureClass.TRANSIENT_EXTERNAL,))
    processor = ContentStatisticsProcessor(retry_policy=retry)

    # _run_request writes its own source-content directory under tmp_path;
    # reuse the one we just seeded real content into by building the request
    # by hand instead of through the shared helper's fresh directory.
    source_catalog_root = tmp_path / "source-catalog"
    source_ref = write_shared_source_catalog(source_catalog_root, items)
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=_portable_local_profiles(),
        limits=WorkLimits(1, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy(
            (DefaultExtractorRegistry.extractor_id,),
            DefaultSegmenterRegistry.segmenter_id,
            (processor.description.processor_id,),
        ),
        processors=ProcessorSet((processor.description,)),
        partition_count=4,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(plan.to_dict()))
    roots = {
        "blobStorage": (tmp_path / "blobs").as_posix(),
        "controlRepository": (tmp_path / "controls").as_posix(),
        "documentCatalog": (tmp_path / "catalog").as_posix(),
        "documentStores": (tmp_path / "stores").as_posix(),
        "reconciliation": (tmp_path / "reconciliation").as_posix(),
        "recordStorage": (tmp_path / "records").as_posix(),
        "sourceCatalog": source_catalog_root.as_posix(),
        "sourceContent": source_content.as_posix(),
    }
    run_request = _write_local_run_request(
        tmp_path / "run-request.json",
        plan_path=plan_path,
        roots=roots,
        result_sink_id="urn:docspec:test:sink:local-durable",
        retry=retry,
        accepted=accepted,
        completed_at="2026-08-05T12:00:00Z",
        max_workers=1,
        max_in_flight=1,
    )

    handoff_reference, handoff = _prepare(tmp_path, run_request)
    stores = LocalDocumentStoreRepository(Path(roots["documentStores"]))
    by_item = {}
    for reference in stores.stream_planned_stores(handoff.planned_store_ledger):
        store = stores.load(reference)
        by_item[store.entries[0].source_item.item_id] = reference

    _execute_task(
        tmp_path,
        run_request=run_request,
        handoff_reference=handoff_reference,
        handoff=handoff,
        plan=plan,
        store_reference=by_item["item-sealed"],
        label="sealed",
    )
    _execute_task(
        tmp_path,
        run_request=run_request,
        handoff_reference=handoff_reference,
        handoff=handoff,
        plan=plan,
        store_reference=by_item["item-active-fail"],
        label="active-fail",
    )

    # item-active-ok needs real acquisition, so it is driven through the
    # exact same executor/delivery services `task execute` uses internally
    # (`_compose_local_run`), just with the shared-fixture fetcher that
    # resolves this test's on-disk content -- the CLI's own composition
    # helper, not a hand-rolled substitute.
    composition = cli_module._compose_local_run(
        cli_module._local_run_request(run_request),
        content_fetcher=SharedFixtureContentFetcher(source_content),
    )
    processed_reference = composition.executor.execute_store(by_item["item-active-ok"])
    composition.delivery.deliver_store(processed_reference, composition.sink_ref)

    # item-running: simulate a worker that claimed the store and died before
    # doing anything else -- call DocumentStore.start() and save it directly,
    # the same transition execute_store() itself makes before touching any
    # entry, without ever reaching a checkpoint or seal.
    running_reference = by_item["item-running"]
    running_store = stores.load(running_reference)
    stores.save(running_store.start("urn:docspec:test:attempt:crash-1"))
    latest = stores.latest(running_reference.store_id)
    assert latest is not None
    revision_path = Path(roots["documentStores"]) / latest.locator
    backdated = time.time() - 3600
    os.utime(revision_path, (backdated, backdated))

    # item-planned is left completely untouched.

    view = _run_active(run_request, capfd, "--stalled-after-seconds", "1800")
    assert view["phase"] == "planned"
    assert view["stores"] == {
        "total": 5,
        "planned": 1,
        "running": 1,
        "sealed": 3,
        "sealedByVerdict": {"completed": 2, "acceptedFailure": 1, "rejected": 0},
        "remaining": 2,
    }
    assert view["entries"]["total"] == 5
    assert view["entries"]["terminal"] == 4
    assert view["entries"]["nonTerminal"] == 1
    assert view["entries"]["byDisposition"] == {
        "accepted-failure": 1,
        "captured": 1,
        "deleted": 2,
    }
    assert view["entries"]["acquisition"] == {"capturedEntries": 1, "newlyCapturedBytes": len(payload)}
    assert view["entries"]["processing"] == {"producedEntries": 1}
    assert view["failures"]["totalRecords"] == 1
    assert view["failures"]["byClassAndDiagnosticCode"] == {
        "transient-external:docspec.acquisition.filenotfounderror": 1,
    }

    progress = view["progress"]
    assert progress["executionStarted"] is True
    assert progress["stalledAfterSeconds"] == 1800
    assert progress["stalledStoreCount"] == 1
    assert progress["stalledStoreSample"] == [
        {
            "storeId": running_reference.store_id,
            "lastObservedAt": progress["stalledStoreSample"][0]["lastObservedAt"],
            "ageSeconds": progress["stalledStoreSample"][0]["ageSeconds"],
        }
    ]
    assert progress["stalledStoreSample"][0]["ageSeconds"] >= 3600
    assert progress["stalledSampleTruncated"] is False

    # A threshold above the crashed store's real age reports no stall at all
    # -- proving the flag is genuinely a function of elapsed time, not a
    # constant "yes" whenever any store is running.
    calm_view = _run_active(run_request, capfd, "--stalled-after-seconds", "7200")
    assert calm_view["progress"]["stalledStoreCount"] == 0
    assert calm_view["progress"]["stalledStoreSample"] == []
    # The same running store is still the most recent progress, regardless
    # of the stall threshold used to evaluate it.
    assert calm_view["progress"]["lastObservedProgressAt"] == progress["lastObservedProgressAt"]

    # The sample list is a bounded, capped preview; the exact count is not.
    # A cap of zero must still report the true count with an empty sample
    # marked truncated, proving the cap bounds the list, not the arithmetic.
    capped_view = _run_active(
        run_request,
        capfd,
        "--stalled-after-seconds",
        "1800",
        "--stalled-sample-limit",
        "0",
    )
    assert capped_view["progress"]["stalledStoreCount"] == 1
    assert capped_view["progress"]["stalledStoreSample"] == []
    assert capped_view["progress"]["stalledSampleTruncated"] is True


def test_run_active_reports_sensibly_for_a_finished_run(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A run that already finished must still read cleanly through this
    command -- nobody has to know in advance which command to reach for."""

    item = SourceItem("solo-item", "v1", (), state=SourceItemState.DELETED)
    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0, max_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    run_request, roots, plan = _run_request(
        tmp_path,
        items=(item,),
        processors=ProcessorSet(()),
        processor_ids=(),
        retry=retry,
        accepted=accepted,
    )
    run_reference_path = tmp_path / "run-reference.json"
    assert (
        main(
            [
                "run",
                "start",
                "--request",
                str(run_request),
                "--destination",
                str(run_reference_path),
                "--receipt",
                str(tmp_path / "run-operation.json"),
            ]
        )
        == 0
    )

    view = _run_active(run_request, capfd)
    assert view["phase"] == "planned"
    assert view["stores"] == {
        "total": 1,
        "planned": 0,
        "running": 0,
        "sealed": 1,
        "sealedByVerdict": {"completed": 1, "acceptedFailure": 0, "rejected": 0},
        "remaining": 0,
    }
    assert view["progress"]["executionStarted"] is True
    assert view["progress"]["stalledStoreCount"] == 0
    assert view["entries"]["terminal"] == 1
    assert view["entries"]["nonTerminal"] == 0
