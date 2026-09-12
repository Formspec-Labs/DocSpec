"""Explicit experiment roots preserve usable shared inputs during inventory."""

import pytest

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.storage import LocalJsonControlRepository, LocalJsonlRecordStorage
from docspec.domain.maintenance import BlobRetentionSet
from docspec.domain.plans import WorkLimits
from docspec.errors import IntegrityError
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import (
    build_local_catalog, build_local_retention_set, open_local_inspection,
    prepare_local_experiment, preview_local_blob_inventory,
)
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from tests.support.source_catalog import producer


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    workspace = LocalWorkspace(tmp_path / "dataset")
    workspace.roots["sourceContent"].mkdir(parents=True)
    (workspace.roots["sourceContent"] / "input.html").write_bytes(
        b"<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
    )
    namespace = "urn:test:retained-roots"
    source = SuppliedRecordSource(({
        "recordId": "document", "sourceIssuedVersion": "1", "title": "Document", "metadata": {},
        "candidateRenditions": [SourceCatalogCandidate("body", "text/html", "immutable-object", "input.html").to_dict()],
    },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
        max_records=10, max_bytes=1024**2)
    catalog = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id=namespace, producer=producer(), max_scratch_bytes=8 * 1024**2)
    fetcher = LocalFileContentFetcher(workspace.roots["sourceContent"])
    calls = []
    fetch = fetcher.fetch

    def counted(*args, **kwargs):
        calls.append(None)
        return fetch(*args, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", counted)
    settings = {
        "limits": WorkLimits(10, 1024**2, 100, 100, 100, 16 * 1024**2, 60),
        "source_catalog_producer": producer(), "document_release_producer": producer(),
        "completed_at": "2026-09-11T00:00:00Z", "deadline_epoch_seconds": 4102444800,
        "content_fetcher": fetcher,
    }

    def prepare(base=None, stop="segmentation", *, completed_at=None):
        stages = {}
        if stop != "capture":
            stages["extractor"] = VisibleTextExtractor()
        if stop == "segmentation":
            stages["segmenter"] = VisibleTextBlockSegmenter()
        selected = settings if completed_at is None else settings | {"completed_at": completed_at}
        return prepare_local_experiment(catalog.reference, workspace, base_release=base,
            stop_after=stop, **selected, **stages)

    return workspace, prepare, calls


def _retained(prepared):
    with prepared:
        return prepared.plan, prepared.retain(prepared.run())


def _build(workspace, plan, **roots):
    return build_local_retention_set(plan, workspace, document_release_producer=producer(),
        max_spooled_bytes=8 * 1024**2, **roots)


def _inventory(workspace, plan, reference, **options):
    return preview_local_blob_inventory(plan, workspace, reference, document_release_producer=producer(),
        max_spooled_bytes=8 * 1024**2, **options)


def _locators(workspace, reference):
    controls = LocalJsonControlRepository(workspace.roots["controlRepository"], create=False)
    records = LocalJsonlRecordStorage(workspace.roots["recordStorage"], create=False)
    retained = BlobRetentionSet.from_dict(controls.load(reference))
    return retained, {row["locator"] for row in records.stream(retained.references)}


def _remove_temporary_candidates(workspace, inventory):
    # Only this test's disposable dataset: prove that a report's selected roots
    # still work after removing objects the report does not reference.
    assert not inventory["candidateSampleTruncated"]
    for candidate in inventory["candidateSample"]:
        (workspace.roots["blobStorage"] / candidate["locator"]).unlink()


def test_shortened_successor_keeps_predecessor_bytes_needed_by_selection(dataset):
    workspace, prepare, calls = dataset
    _, base = _retained(prepare(stop="capture"))
    deep_plan, deep = _retained(prepare(base))
    short_plan, short = _retained(prepare(deep, stop="capture"))
    deep_refs = _locators(workspace, _build(workspace, deep_plan, retained_releases=(deep,)))[1]
    short_set = _build(workspace, short_plan, retained_releases=(short,))
    retained, short_refs = _locators(workspace, short_set)
    assert retained.retained_releases == (short,)
    assert retained.verification_evidence["catalogVerifiedReleaseCount"] == 3
    assert short_refs == deep_refs and len(short_refs) > 1
    assert _inventory(workspace, short_plan, short_set)["candidateCount"] == 0
    # Exercise the actual predecessor-admitting operation, not just row reading.
    from docspec.runtime.storage import _local_profiles, _local_storage
    *_, catalog = _local_storage(workspace.roots, _local_profiles(short_plan, workspace), producer(), create=False)
    assert catalog.select(short, expected_current=None) == short
    assert calls == [None]


def test_standalone_planned_checkpoint_requires_its_plan_and_keeps_base_inputs(dataset):
    workspace, prepare, calls = dataset
    _, base = _retained(prepare(stop="capture"))
    with prepare(base) as pending:
        tasks = tuple(pending.task_source(pending.handoff))
        roots = {"retained_stores": tuple(task.input_store for task in tasks)}
        with pytest.raises(IntegrityError, match="explicit processing-plan"):
            _build(workspace, pending.plan, **roots)
        reference = _build(workspace, pending.plan, retained_plans=(pending.handoff.processing_plan,), **roots)
        retained, locators = _locators(workspace, reference)
        assert retained.retained_plans == (pending.handoff.processing_plan,)
        assert retained.verification_evidence["catalogVerifiedReleaseCount"] == 1
        assert len(locators) == 1
        _remove_temporary_candidates(workspace, _inventory(workspace, pending.plan, reference))
        result = pending.retain(pending.run())
        view = open_local_inspection(pending.plan, workspace, document_release_producer=producer(), release_ref=result)
        assert view.summary()["result"]["layers"]["segments"] == 2
    assert calls == [None]


def test_unselected_sibling_bytes_can_be_omitted_without_damaging_shared_base(dataset):
    workspace, prepare, calls = dataset
    _, base = _retained(prepare(stop="capture"))
    first_plan, first = _retained(prepare(base))
    second_plan, second = _retained(prepare(base, stop="extraction"))
    together = _build(workspace, second_plan, retained_releases=(first, second))
    second_only = _build(workspace, second_plan, retained_releases=(second,))
    assert len(_locators(workspace, together)[1]) > len(_locators(workspace, second_only)[1])
    inventory = _inventory(workspace, second_plan, second_only)
    assert inventory["candidateCount"] > 0
    _remove_temporary_candidates(workspace, inventory)
    view = open_local_inspection(second_plan, workspace, document_release_producer=producer(), release_ref=second)
    assert view.summary()["result"]["layers"]["representations"] == 1
    assert _inventory(workspace, second_plan, second_only)["candidateCount"] == 0
    # The kept alternative can still supply a later processing attempt.
    _retained(prepare(second))
    assert calls == [None]
    assert first_plan != second_plan


def test_interrupted_preview_is_read_only_and_repeatable(dataset, monkeypatch):
    workspace, prepare, _ = dataset
    plan, result = _retained(prepare())
    reference = _build(workspace, plan, retained_releases=(result,))
    before = {path: path.read_bytes() for path in workspace.root.rglob("*") if path.is_file()}

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("interrupted inventory")

    with monkeypatch.context() as patch:
        patch.setattr("docspec.adapters.blob_inventory.os.scandir", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _inventory(workspace, plan, reference)
    assert {path: path.read_bytes() for path in workspace.root.rglob("*") if path.is_file()} == before
    assert _inventory(workspace, plan, reference)["candidateCount"] == 0
    assert _build(workspace, plan, retained_releases=(result,)) == reference


def test_distinct_retained_artifacts_of_one_logical_plan_are_both_valid_roots(dataset):
    workspace, prepare, _ = dataset
    first_plan, first = _retained(prepare())
    second_plan, second = _retained(prepare(completed_at="2026-09-12T00:00:00Z"))
    assert first_plan == second_plan
    assert first.release_id == second.release_id and first.digest != second.digest
    reference = _build(workspace, second_plan, retained_releases=(first, second))
    retained, _ = _locators(workspace, reference)
    assert set(retained.retained_releases) == {first, second}
    assert retained.verification_evidence["catalogVerifiedReleaseCount"] == 2
    assert _inventory(workspace, second_plan, reference)["candidateCount"] == 0
