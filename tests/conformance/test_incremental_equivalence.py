
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import (
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.maintenance import ReleaseCompactionService, logical_release_state_digest
from docspec.application.planner import RunPlanner
from docspec.domain.content import AcquisitionDisposition, SourceItem
from docspec.domain.jobs import ChangeKind, EntryExecutionMode, FailureClass
from docspec.domain.maintenance import ReleaseCompactionReceipt
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.references import DocumentReleaseRef
from tests.support import pipeline as _pipeline_helpers
from tests.support import processors as _processor_helpers
from tests.support.incremental import (
    _active_document_state,
    _helpers,
    _platform,
    document_release_producer,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


SharedFixtureContentFetcher = _helpers.SharedFixtureContentFetcher

_run = _pipeline_helpers._run
_write_source = _pipeline_helpers._write_source
_CountingExtractor = _processor_helpers._CountingExtractor
_CountingFetcher = _processor_helpers._CountingFetcher
_CountingProcessor = _processor_helpers._CountingProcessor
_CountingSegmenter = _processor_helpers._CountingSegmenter
_description = _processor_helpers._description
_plan = _processor_helpers._plan


def _targeted_plan(
    source,
    base,
    processors,
    retry: RetryPolicy,
    accepted: AcceptedFailurePolicy,
    *,
    selection: dict[str, Any],
):
    """The shared plan shape with an explicit targeted selection."""

    untargeted = _plan(source, base, processors, retry, accepted)
    return type(untargeted).create(
        source_catalog=untargeted.source_catalog,
        base_release=untargeted.base_release,
        profiles=untargeted.profiles,
        limits=untargeted.limits,
        stages=untargeted.stages,
        processors=untargeted.processors,
        partition_count=untargeted.partition_count,
        selection=selection,
        retention_policy=untargeted.retention_policy,
        data_use_policy=untargeted.data_use_policy,
        retry_policy_digest=untargeted.retry_policy_digest,
        accepted_failure_policy_digest=untargeted.accepted_failure_policy_digest,
    )


_REPAIR_FIXTURE = (
    # Strictly ordered by item id: source-native records require it.
    ("document-broken", "broken.txt", "Broken starts out failing its processor."),
    ("document-steady-a", "steady-a.txt", "Steady A never changes across either run."),
    ("document-steady-b", "steady-b.txt", "Steady B never changes across either run either."),
)


def _repair_items(sources: Path) -> tuple[SourceItem, ...]:
    """Write the shared repair fixture below one platform and name its items.

    The locator a candidate carries is its file name, not its absolute path,
    so the same population written below two different roots produces
    byte-identical source-item records and its releases stay comparable.
    """

    return tuple(
        SourceItem(item_id, "v1", (_write_source(sources / name, text),), metadata={"expectedSegments": 1})
        for item_id, name, text in _REPAIR_FIXTURE
    )


def _terminal_dispositions(
    catalog: LocalManifestDocumentCatalog,
    release_ref: DocumentReleaseRef,
) -> dict[str, str | None]:
    """Project the one terminal disposition each source item ended a release with."""

    projected: dict[str, str | None] = {}
    for row in catalog.scan(release_ref, layer_kind="dispositions"):
        # A release carries exactly one disposition row per item; a second one
        # would mean a superseded verdict survived, so refuse rather than
        # silently keep whichever came last.
        assert row["sourceItemId"] not in projected, "release repeats a terminal disposition"
        projected[row["sourceItemId"]] = row["payload"]["disposition"]
    return projected


def _failed_item_ids(
    catalog: LocalManifestDocumentCatalog,
    release_ref: DocumentReleaseRef,
) -> set[str]:
    """Name every source item carrying failure evidence, terminal or retried-past."""

    return {row["sourceItemId"] for row in catalog.scan(release_ref, layer_kind="failures")}


def _planned_entries(platform, plan, source_reference, base_release, *, name: str) -> tuple:
    """Plan one run against a committed base release without executing it.

    Planning-time properties -- which items a second run re-admits and which
    it drops -- are provable without a second capture/process/deliver/commit
    pass, so these tests exercise ``RunPlanner`` directly.
    """

    plan_reference = platform.controls.put(
        kind="plans",
        artifact_id=plan.plan_id,
        value=plan.to_dict(),
    )
    planned = tuple(
        RunPlanner(
            source_catalog=platform.source_catalog,
            document_catalog=platform.catalog,
            stores=platform.stores,
            controls=platform.controls,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                platform.records.root / f".planning-{name}",
                read_batch_size=1,
            ),
        ).plan_run(source_reference, base_release, plan_reference)
    )
    return tuple(
        entry
        for reference in planned
        for entry in platform.stores.load(reference).entries
    )


def test_clean_incremental_targeted_and_compacted_paths_converge_on_active_document_state(
    tmp_path: Path,
) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()

    evolving = _platform(tmp_path / "evolving", member_bytes=3700)
    first_a = _write_source(
        evolving.sources / "a.txt",
        "Alpha before the update in its initial paragraph.",
    )
    first_b = _write_source(
        evolving.sources / "b.txt",
        "Bravo remains stable in its only paragraph.",
    )
    first_i = _write_source(
        evolving.sources / "i.txt",
        "India shares a storage partition and remains stable.",
    )
    first_source = evolving.publish_source(
        (
                SourceItem("document-a", "v1", (first_a,), metadata={"expectedSegments": 1}),
                SourceItem("document-b", "v1", (first_b,), metadata={"expectedSegments": 1}),
                SourceItem("document-i", "v1", (first_i,), metadata={"expectedSegments": 1}),
        )
    )
    initial_processor = _CountingProcessor(_description("equivalence", "1", retry))
    initial_plan = _plan(first_source, None, (initial_processor,), retry, accepted)
    _, _, _, _, initial_release = _run(
        plan=initial_plan,
        source_catalog=evolving.source_catalog,
        controls=evolving.controls,
        stores=evolving.stores,
        blobs=evolving.blobs,
        records=evolving.records,
        catalog=evolving.catalog,
        fetcher=SharedFixtureContentFetcher(evolving.sources),
        processors=(initial_processor,),
        partition_policy=evolving.partition_policy,
    )

    final_a = _write_source(
        evolving.sources / "a.txt",
        "Alpha after the update in its final paragraph.",
    )
    final_c = _write_source(
        evolving.sources / "c.txt",
        "Charlie is newly added with one final paragraph.",
    )
    final_items = (
        SourceItem("document-a", "v2", (final_a,), metadata={"expectedSegments": 1}),
        SourceItem("document-b", "v1", (first_b,), metadata={"expectedSegments": 1}),
        SourceItem("document-c", "v1", (final_c,), metadata={"expectedSegments": 1}),
        SourceItem("document-i", "v1", (first_i,), metadata={"expectedSegments": 1}),
    )
    final_source = evolving.publish_source(final_items, name="final")
    final_processor = _CountingProcessor(_description("equivalence", "2", retry))
    fetcher = _CountingFetcher(SharedFixtureContentFetcher(evolving.sources))
    extractor = _CountingExtractor()
    segmenter = _CountingSegmenter()
    incremental_plan = _plan(final_source, initial_release, (final_processor,), retry, accepted)
    planned, _, _, _, incremental_release = _run(
        plan=incremental_plan,
        source_catalog=evolving.source_catalog,
        controls=evolving.controls,
        stores=evolving.stores,
        blobs=evolving.blobs,
        records=evolving.records,
        catalog=evolving.catalog,
        fetcher=fetcher,
        processors=(final_processor,),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=evolving.partition_policy,
    )

    entries = {
        entry.source_item.item_id: entry
        for reference in planned
        for entry in evolving.stores.load(reference).entries
    }
    assert (entries["document-a"].change, entries["document-a"].execution_mode) == (
        ChangeKind.CHANGED,
        EntryExecutionMode.FULL,
    )
    assert (entries["document-b"].change, entries["document-b"].execution_mode) == (
        ChangeKind.REPAIR,
        EntryExecutionMode.FROM_SEGMENTS,
    )
    assert (entries["document-c"].change, entries["document-c"].execution_mode) == (
        ChangeKind.ADDED,
        EntryExecutionMode.FULL,
    )
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == (2, 2, 2)

    def _clean_state(
        root: Path,
        *,
        alpha_text: str,
        alpha_version: str,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        clean = _platform(root, member_bytes=1024 * 1024)
        clean_items = (
            SourceItem(
                "document-a",
                alpha_version,
                (_write_source(clean.sources / "a.txt", alpha_text),),
                metadata={"expectedSegments": 1},
            ),
            SourceItem(
                "document-b",
                "v1",
                (
                    _write_source(
                        clean.sources / "b.txt",
                        "Bravo remains stable in its only paragraph.",
                    ),
                ),
                metadata={"expectedSegments": 1},
            ),
                SourceItem(
                    "document-c",
                "v1",
                (
                    _write_source(
                        clean.sources / "c.txt",
                        "Charlie is newly added with one final paragraph.",
                    ),
                ),
                    metadata={"expectedSegments": 1},
                ),
                SourceItem(
                    "document-i",
                    "v1",
                    (
                        _write_source(
                            clean.sources / "i.txt",
                            "India shares a storage partition and remains stable.",
                        ),
                    ),
                    metadata={"expectedSegments": 1},
                ),
        )
        clean_source = clean.publish_source(clean_items)
        clean_processor = _CountingProcessor(_description("equivalence", "2", retry))
        clean_plan = _plan(clean_source, None, (clean_processor,), retry, accepted)
        _, _, _, _, clean_release = _run(
            plan=clean_plan,
            source_catalog=clean.source_catalog,
            controls=clean.controls,
            stores=clean.stores,
            blobs=clean.blobs,
            records=clean.records,
            catalog=clean.catalog,
            fetcher=SharedFixtureContentFetcher(clean.sources),
            processors=(clean_processor,),
            partition_policy=clean.partition_policy,
        )
        return _active_document_state(clean.catalog, clean_release)

    assert _clean_state(
        tmp_path / "clean-incremental",
        alpha_text="Alpha after the update in its final paragraph.",
        alpha_version="v2",
    ) == _active_document_state(evolving.catalog, incremental_release)

    # Targeted pass: only document-a changes again, and the plan selects it
    # explicitly; unrelated content must be neither refetched nor reprocessed.
    targeted_text = "Alpha targeted once more in its last paragraph."
    targeted_a = _write_source(evolving.sources / "a.txt", targeted_text)
    targeted_source = evolving.publish_source(
        (
            SourceItem("document-a", "v3", (targeted_a,), metadata={"expectedSegments": 1}),
                final_items[1],
                final_items[2],
                final_items[3],
        ),
        name="targeted",
    )
    targeted_processor = _CountingProcessor(_description("equivalence", "2", retry))
    targeted_fetcher = _CountingFetcher(SharedFixtureContentFetcher(evolving.sources))
    targeted_extractor = _CountingExtractor()
    targeted_segmenter = _CountingSegmenter()
    targeted_plan = _targeted_plan(
        targeted_source,
        incremental_release,
        (targeted_processor,),
        retry,
        accepted,
        selection={"includeItemIds": ["document-a"]},
    )
    targeted_planned, _, _, _, targeted_release = _run(
        plan=targeted_plan,
        source_catalog=evolving.source_catalog,
        controls=evolving.controls,
        stores=evolving.stores,
        blobs=evolving.blobs,
        records=evolving.records,
        catalog=evolving.catalog,
        fetcher=targeted_fetcher,
        processors=(targeted_processor,),
        extractor=targeted_extractor,
        segmenter=targeted_segmenter,
        partition_policy=evolving.partition_policy,
    )
    targeted_entries = tuple(
        entry
        for reference in targeted_planned
        for entry in evolving.stores.load(reference).entries
    )
    assert [entry.source_item.item_id for entry in targeted_entries] == ["document-a"]
    assert (targeted_entries[0].change, targeted_entries[0].execution_mode) == (
        ChangeKind.CHANGED,
        EntryExecutionMode.FULL,
    )
    assert (len(targeted_fetcher.calls), targeted_extractor.calls, targeted_segmenter.calls) == (1, 1, 1)

    compacted_records = LocalJsonlRecordStorage(
        evolving.records.root,
        max_member_bytes=1024 * 1024,
    )
    compacting_catalog = LocalManifestDocumentCatalog(
        evolving.catalog.root,
        records=compacted_records,
        stores=evolving.stores,
        controls=evolving.controls,
        producer=document_release_producer(),
        blobs=evolving.blobs,
    )
    receipt_ref = ReleaseCompactionService(
        controls=evolving.controls,
        records=compacted_records,
        stores=evolving.stores,
        document_catalog=compacting_catalog,
        clock=lambda: "2026-08-05T13:00:00Z",
    ).compact(targeted_release)
    receipt = ReleaseCompactionReceipt.from_dict(evolving.controls.load(receipt_ref))
    compacted_release = receipt.successor_release

    clean_targeted_state = _clean_state(
        tmp_path / "clean-targeted",
        alpha_text=targeted_text,
        alpha_version="v3",
    )
    targeted_state = _active_document_state(compacting_catalog, targeted_release)
    compacted_state = _active_document_state(compacting_catalog, compacted_release)
    assert clean_targeted_state == targeted_state == compacted_state

    targeted = compacting_catalog.open(targeted_release)
    compacted = compacting_catalog.open(compacted_release)
    assert receipt.source_logical_state_digest == receipt.successor_logical_state_digest
    assert logical_release_state_digest(compacted_records, targeted) == logical_release_state_digest(
        compacted_records,
        compacted,
    )
    assert all(
        list(compacting_catalog.compare(targeted_release, compacted_release, layer_kind=layer.layer_kind)) == []
        for layer in targeted.active_layers
    )


def test_a_previously_failed_item_is_replanned_as_repair_and_unfailed_items_stay_dropped(
    tmp_path: Path,
) -> None:
    """Explicit repair reuses a failed item's completed stages and leaves
    successful unchanged items out of the next plan. An unchanged deterministic
    failure remains visible until the caller requests retry or changes its
    unfinished work."""

    _FailingProcessor = importlib.import_module("tests.support.store_results")._FailingProcessor

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy((FailureClass.DETERMINISTIC_INPUT,))
    platform = _platform(tmp_path / "repair", member_bytes=1024 * 1024)

    items = _repair_items(platform.sources)
    description = _description("repair-failed-item", "1", retry)

    # First run: "document-broken" fails its processor deterministically and
    # is accepted as a failure rather than blocking the release.
    first_source = platform.publish_source(items, name="first")
    failing_processor = _FailingProcessor(
        description,
        fail_item_id="document-broken",
        error=ValueError("declared deterministic fixture failure"),
    )
    first_plan = _plan(first_source, None, (failing_processor,), retry, accepted)
    first_planned, _, first_sealed, _, first_release = _run(
        plan=first_plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=SharedFixtureContentFetcher(platform.sources),
        processors=(failing_processor,),
        partition_policy=platform.partition_policy,
        accepted_failure_policy=accepted,
    )

    first_planned_entries = {
        entry.source_item.item_id: entry
        for reference in first_planned
        for entry in platform.stores.load(reference).entries
    }
    assert set(first_planned_entries) == {"document-steady-a", "document-steady-b", "document-broken"}
    assert {entry.change for entry in first_planned_entries.values()} == {ChangeKind.ADDED}

    first_sealed_entries = {
        entry.source_item.item_id: entry
        for reference in first_sealed
        for entry in platform.stores.load(reference).entries
    }
    assert first_sealed_entries["document-broken"].disposition == AcquisitionDisposition.ACCEPTED_FAILURE
    assert first_sealed_entries["document-steady-a"].disposition == AcquisitionDisposition.CAPTURED
    assert first_sealed_entries["document-steady-b"].disposition == AcquisitionDisposition.CAPTURED

    # Unchanged deterministic failures are held by default, alongside the
    # successful items that need no further work.
    second_source = platform.publish_source(items, name="second")
    unchanged_plan = _plan(second_source, first_release, (failing_processor,), retry, accepted)
    assert unchanged_plan.governing_content() == first_plan.governing_content(), (
        "the scenario requires an unchanged plan; only the base release and "
        "source-catalog reference may legitimately differ"
    )
    assert _planned_entries(platform, unchanged_plan, second_source, first_release, name="held") == ()

    second_plan = _targeted_plan(
        second_source, first_release, (failing_processor,), retry, accepted,
        selection={"retryFailures": "selected"},
    )
    # Explicit retry touches only the failed item in the committed release.
    second_planned_entries = _planned_entries(
        platform,
        second_plan,
        second_source,
        first_release,
        name="second",
    )
    # The property under test: the plan touches exactly the one previously
    # failed item and nothing else — asserted on the touched-item count, not
    # merely its classification.
    assert len(second_planned_entries) == 1
    (repaired,) = second_planned_entries
    assert repaired.source_item.item_id == "document-broken"
    assert repaired.change == ChangeKind.REPAIR
    assert repaired.execution_mode == EntryExecutionMode.FROM_SEGMENTS
    assert repaired.processor_ids_to_run == (description.processor_id,)

    # The negative: the two items that succeeded the first time are still
    # UNCHANGED and still dropped from the plan.
    assert {entry.source_item.item_id for entry in second_planned_entries} == {"document-broken"}

    # Changing the failed processor is relevant repair intent. All three items
    # already completed capture, extraction, and segmentation, so each can use
    # those saved segments for the replacement processor.
    bumped_processor = _CountingProcessor(_description("repair-failed-item", "2", retry))
    bumped_plan = _plan(second_source, first_release, (bumped_processor,), retry, accepted)
    assert {
        entry.source_item.item_id: (entry.change, entry.execution_mode)
        for entry in _planned_entries(platform, bumped_plan, second_source, first_release, name="bumped")
    } == {
        "document-broken": (ChangeKind.REPAIR, EntryExecutionMode.FROM_SEGMENTS),
        "document-steady-a": (ChangeKind.REPAIR, EntryExecutionMode.FROM_SEGMENTS),
        "document-steady-b": (ChangeKind.REPAIR, EntryExecutionMode.FROM_SEGMENTS),
    }

    # The repaired item completes without fetching or rerunning the completed
    # stages. The result still contains every item's complete active state.
    healed_processor = _CountingProcessor(description)
    healed_fetcher = _CountingFetcher(SharedFixtureContentFetcher(platform.sources))
    healed_extractor, healed_segmenter = _CountingExtractor(), _CountingSegmenter()
    _, _, second_sealed, _, second_release = _run(
        plan=second_plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=healed_fetcher,
        processors=(healed_processor,),
        extractor=healed_extractor,
        segmenter=healed_segmenter,
        partition_policy=platform.partition_policy,
        accepted_failure_policy=accepted,
    )
    second_sealed_entries = {
        entry.source_item.item_id: entry
        for reference in second_sealed
        for entry in platform.stores.load(reference).entries
    }
    assert set(second_sealed_entries) == {"document-broken"}
    assert second_sealed_entries["document-broken"].disposition == AcquisitionDisposition.CAPTURED
    assert healed_fetcher.calls == []
    assert healed_extractor.calls == healed_segmenter.calls == 0
    assert len(healed_processor.calls) == 1

    # The repaired release must equal a clean build over the same population,
    # not merely mention the same three ids: every file, representation,
    # segment and derived record has to match, or the repair left partial
    # state behind from the run that failed.
    clean = _platform(tmp_path / "clean-repair", member_bytes=1024 * 1024)
    clean_processor = _CountingProcessor(description)
    clean_source = clean.publish_source(_repair_items(clean.sources))
    _, _, _, _, clean_release = _run(
        plan=_plan(clean_source, None, (clean_processor,), retry, accepted),
        source_catalog=clean.source_catalog,
        controls=clean.controls,
        stores=clean.stores,
        blobs=clean.blobs,
        records=clean.records,
        catalog=clean.catalog,
        fetcher=SharedFixtureContentFetcher(clean.sources),
        processors=(clean_processor,),
        partition_policy=clean.partition_policy,
        accepted_failure_policy=accepted,
    )
    assert _active_document_state(platform.catalog, second_release) == _active_document_state(
        clean.catalog,
        clean_release,
    )
    # _active_document_state deliberately omits the two execution-evidence
    # layers, so compare those separately in the only form in which a
    # repaired release and a clean one are comparable: the disposition each
    # item ended with, and the fact that repairing cleared the stale failure
    # evidence rather than accumulating it.
    assert _terminal_dispositions(platform.catalog, second_release) == _terminal_dispositions(
        clean.catalog,
        clean_release,
    ) == {
        "document-broken": AcquisitionDisposition.CAPTURED.value,
        "document-steady-a": AcquisitionDisposition.CAPTURED.value,
        "document-steady-b": AcquisitionDisposition.CAPTURED.value,
    }
    assert _failed_item_ids(platform.catalog, first_release) == {"document-broken"}
    assert _failed_item_ids(platform.catalog, second_release) == set()

    # And it converges: with the repair captured, a third run over the same
    # unchanged catalog and plan plans nothing at all. A repair that kept
    # re-admitting its own item would loop here instead.
    third_source = platform.publish_source(items, name="third")
    third_plan = _plan(third_source, second_release, (healed_processor,), retry, accepted)
    assert _planned_entries(platform, third_plan, third_source, second_release, name="third") == ()


def test_an_item_that_failed_once_and_then_succeeded_is_not_replanned(tmp_path: Path) -> None:
    """Failure evidence is not a terminal verdict. A capture that loses its
    first transport, retries, and succeeds publishes a `failures` row beside a
    `captured` disposition -- see
    tests/conformance/test_acquisition.py::test_a_transport_retry_reconciles_into_a_published_capture,
    which requires that evidence to survive into the release. Planning off the
    presence of a failure row would therefore re-admit every item that ever
    hiccupped, at FULL execution, refetching bytes the release already holds;
    with transient transport loss common, that silently turns an incremental
    run into a near-full rebuild. The next run must classify such an item
    UNCHANGED and drop it."""

    _FailFirstFetchFetcher = importlib.import_module(
        "tests.conformance.test_acquisition"
    )._FailFirstFetchFetcher

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    platform = _platform(tmp_path / "retried", member_bytes=1024 * 1024)
    flaky = _write_source(platform.sources / "flaky.txt", "Flaky loses its first transport, then arrives.")
    steady = _write_source(platform.sources / "steady.txt", "Steady arrives the first time.")
    items = (
        SourceItem("document-flaky", "v1", (flaky,), metadata={"expectedSegments": 1}),
        SourceItem("document-steady", "v1", (steady,), metadata={"expectedSegments": 1}),
    )
    description = _description("retried-then-succeeded", "1", retry)

    first_processor = _CountingProcessor(description)
    first_source = platform.publish_source(items, name="first")
    first_plan = _plan(first_source, None, (first_processor,), retry, accepted)
    _, _, first_sealed, _, first_release = _run(
        plan=first_plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=_FailFirstFetchFetcher(
            SharedFixtureContentFetcher(platform.sources),
            flaky_locator="flaky.txt",
        ),
        processors=(first_processor,),
        partition_policy=platform.partition_policy,
        accepted_failure_policy=accepted,
    )

    # The premise: the flaky item both succeeded and left failure evidence.
    first_entries = {
        entry.source_item.item_id: entry
        for reference in first_sealed
        for entry in platform.stores.load(reference).entries
    }
    assert first_entries["document-flaky"].disposition == AcquisitionDisposition.CAPTURED
    assert [failure.failure_class for failure in first_entries["document-flaky"].failures] == [
        FailureClass.TRANSIENT_EXTERNAL
    ]
    assert _failed_item_ids(platform.catalog, first_release) == {"document-flaky"}
    assert _terminal_dispositions(platform.catalog, first_release) == {
        "document-flaky": AcquisitionDisposition.CAPTURED.value,
        "document-steady": AcquisitionDisposition.CAPTURED.value,
    }

    # The property: nothing is repairable, so the second run plans nothing.
    second_source = platform.publish_source(items, name="second")
    second_plan = _plan(second_source, first_release, (first_processor,), retry, accepted)
    assert second_plan.governing_content() == first_plan.governing_content()
    assert _planned_entries(platform, second_plan, second_source, first_release, name="second") == ()
