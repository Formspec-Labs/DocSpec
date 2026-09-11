
from __future__ import annotations

from pathlib import Path

import pytest

from docspec.domain.content import SourceItem
from docspec.domain.jobs import FailureClass, StoreVerdict
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import StoreRef
from docspec.errors import StateTransitionError
from tests import helpers as _helpers
from tests.support import incremental as _equivalence
from tests.support import pipeline as _pipeline_helpers
from tests.support.store_results import (
    _FailingProcessor,
    _processor_helpers,
    _reconciled_counts,
)

ROOT = Path(__file__).resolve().parents[2]


SharedFixtureContentFetcher = _helpers.SharedFixtureContentFetcher

_platform = _equivalence._platform
_run = _pipeline_helpers._run
_write_source = _pipeline_helpers._write_source
_description = _processor_helpers._description
_plan = _processor_helpers._plan


def _seeded_run(root: Path, *, items: tuple[str, ...], processor, accepted: AcceptedFailurePolicy):
    retry = RetryPolicy(base_delay_milliseconds=0)
    platform = _platform(root, member_bytes=1024 * 1024)
    source_items = []
    for item_id in sorted(items):
        candidate = _write_source(platform.sources / f"{item_id}.txt", f"{item_id} holds one paragraph.")
        source_items.append(SourceItem(item_id, "v1", (candidate,), metadata={"expectedSegments": 1}))
    source = platform.publish_source(tuple(source_items))
    plan = _plan(source, None, (processor,), retry, accepted)
    return platform, plan


def test_completed_and_accepted_failure_stores_reconcile_with_complete_counts(tmp_path: Path) -> None:
    """One run seals a completed store and an accepted-failure store; the run
    receipt's resource and disposition counts must equal an independent
    recount of the sealed store receipts, and the release must carry the
    failure evidence."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy((FailureClass.DETERMINISTIC_INPUT,))
    processor = _FailingProcessor(
        _description("store-verdicts", "1", retry),
        fail_item_id="document-af",
        error=ValueError("declared deterministic fixture failure"),
    )
    platform, plan = _seeded_run(
        tmp_path / "accepted",
        items=("document-ok", "document-af"),
        processor=processor,
        accepted=accepted,
    )
    planned, _, sealed, run_ref, release_ref = _run(
        plan=plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=SharedFixtureContentFetcher(platform.sources),
        processors=(processor,),
        partition_policy=platform.partition_policy,
        accepted_failure_policy=accepted,
    )
    assert len(planned) == 2, "the fixture items must plan into two separate stores"
    run = RunReceipt.from_dict(platform.controls.load(run_ref))

    verdicts = {
        store.entries[0].source_item.item_id: store.verdict
        for store in (platform.stores.load(reference) for reference in sealed)
    }
    assert verdicts == {
        "document-ok": StoreVerdict.COMPLETED,
        "document-af": StoreVerdict.ACCEPTED_FAILURE,
    }

    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["stores"] == 2
    assert run.counts["selectedItems"] == 2
    assert run.counts["acceptedFailureStores"] == 1
    assert run.counts["rejectedStores"] == 0
    assert run.counts["derivedRecords"] == 1, "the failed entry produced no derived record"
    assert run.failures["counts"] == {FailureClass.DETERMINISTIC_INPUT.value: 1}
    assert run.failures["first"]["failureClass"] == FailureClass.DETERMINISTIC_INPUT.value
    assert run.failures["first"]["attempt"] == 1
    assert run.failures["first"]["retryable"] is False

    release = platform.catalog.open(release_ref)
    assert release.failures["counts"] == run.failures["counts"], (
        "the release must carry the reconciled failure counts"
    )
    assert release.failures["first"]["failureClass"] == run.failures["first"]["failureClass"]
    assert release.failures["first"]["diagnosticCode"] == run.failures["first"]["diagnosticCode"]
    assert release.failures["first"]["entryId"], "the release pins the failing entry identity"


def test_a_rejected_store_reconciles_but_refuses_publication(tmp_path: Path) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = _FailingProcessor(
        _description("store-verdicts", "1", retry),
        fail_item_id="document-rj",
        error=RuntimeError("undeclared fixture defect"),
    )
    platform, plan = _seeded_run(
        tmp_path / "rejected",
        items=("document-rj",),
        processor=processor,
        accepted=AcceptedFailurePolicy(),
    )
    with pytest.raises(StateTransitionError, match="rejected stores cannot publish"):
        _run(
            plan=plan,
            source_catalog=platform.source_catalog,
            controls=platform.controls,
            stores=platform.stores,
            blobs=platform.blobs,
            records=platform.records,
            catalog=platform.catalog,
            fetcher=SharedFixtureContentFetcher(platform.sources),
            processors=(processor,),
            partition_policy=platform.partition_policy,
        )
    assert platform.catalog.current() is None, "a rejected run must not advance the catalog"

    receipts = sorted((platform.controls.root / "control" / "run-receipts").rglob("*.json"))
    assert len(receipts) == 1, "reconciliation must still seal one complete run receipt"
    import json

    run = RunReceipt.from_dict(json.loads(receipts[0].read_text(encoding="utf-8"))["value"])
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["rejectedStores"] == 1
    assert run.counts["derivedRecords"] == 0
    assert run.failures["counts"] == {FailureClass.IMPLEMENTATION_DEFECT.value: 1}

    store = platform.stores.load(StoreRef.from_dict(next(iter(platform.records.stream(run.store_ledger)))["store"]))
    assert store.verdict is StoreVerdict.REJECTED
    assert store.entries[0].failures, "a rejected entry must retain its complete failure records"
