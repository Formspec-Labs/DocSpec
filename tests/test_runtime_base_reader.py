"""A prepared worker admits its immutable base once and keeps checking used evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
import json
from threading import Barrier

import pytest

from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import EntryExecutionMode
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef, BlobRef
from docspec.errors import IntegrityError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import open_local_inspection, prepare_local_experiment
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.support.profiles import _seeded_local_run_arguments


@pytest.fixture
def retained_experiment(tmp_path, monkeypatch):
    original = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    workspace = original["workspace"]
    items = []
    for name in ("document-a", "document-b", "document-c"):
        content = f"First paragraph of {name}.\n\nSecond paragraph.".encode()
        (workspace.roots["sourceContent"] / name).write_bytes(content)
        items.append(SourceItem(name, "v1", (CandidateFile(
            "primary", name, "text/plain", expected_size=len(content),
            expected_digest=sha256_digest(content), transport_version="fixture:v1",
        ),), metadata={"expectedSegments": 2}))
    source = write_shared_source_catalog(workspace.roots["sourceCatalog"], tuple(items))
    fetcher = SharedFixtureContentFetcher(workspace.roots["sourceContent"])
    settings = {key: original[key] for key in (
        "retry_policy", "accepted_failure_policy", "source_catalog_producer", "document_release_producer",
        "deadline_epoch_seconds", "completed_at",
    )} | {"limits": replace(original["plan"].limits, max_entries=1),
         "content_fetcher": fetcher, "extractor": TextExtractor()}
    with prepare_local_experiment(source, workspace, stop_after="extraction", **settings) as capture:
        base = capture.retain(capture.run())

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("later segmentation must use the retained bytes")

    monkeypatch.setattr(fetcher, "fetch", unexpected_fetch)
    monkeypatch.setattr(settings["extractor"], "extract", unexpected_fetch)

    def prepare(**changes):
        return prepare_local_experiment(source, workspace, **(settings | {
            "stop_after": "segmentation", "segmenter": ParagraphSegmenter(), "base_release": base,
        } | changes))

    return prepare, base


def _tasks(prepared):
    tasks = tuple(prepared.task_source(prepared.handoff))
    assert len(tasks) == 3
    for task in tasks:
        entry, = prepared._composition.stores.load(task.input_store).entries
        assert entry.execution_mode is EntryExecutionMode.FROM_REPRESENTATIONS
    return tasks


def _observe_admission(prepared, monkeypatch):
    opened = []
    open_reader = prepared._composition.catalog.open_reader

    def observed(reference):
        opened.append(reference)
        return open_reader(reference)

    monkeypatch.setattr(prepared._composition.catalog, "open_reader", observed)
    return opened


@pytest.mark.parametrize("threaded", [False, True])
def test_many_real_processing_tasks_share_one_admission_and_keep_complete_results(
    retained_experiment, monkeypatch, threaded,
):
    prepare, base = retained_experiment
    with prepare() as prepared:
        tasks = _tasks(prepared)
        opened = _observe_admission(prepared, monkeypatch)
        if threaded:
            ready = Barrier(len(tasks))
            reprocessing_reader = prepared._composition.executor._reprocessing_reader

            def concurrent_admission(store, plan):
                ready.wait(timeout=10)
                return reprocessing_reader(store, plan)

            monkeypatch.setattr(prepared._composition.executor, "_reprocessing_reader", concurrent_admission)
            with ThreadPoolExecutor(max_workers=len(tasks)) as workers:
                results = list(workers.map(lambda task: prepared.execute_task(prepared.handoff, task), tasks))
        else:
            results = [prepared.execute_task(prepared.handoff, task) for task in tasks]
        assert opened == [base]
        run = prepared.reconcile(results)
        view = open_local_inspection(prepared.plan, prepared._composition.workspace,
            document_release_producer=prepared._composition.catalog.producer, run_ref=run)
        counts = view.summary()["work"]["counts"]
        assert counts["newCapturedFiles"] == counts["newRepresentations"] == 0
        assert counts["newSegments"] == 6
        retained = prepared.retain(run)
        reader = prepared._composition.catalog.open_reader(retained)
        assert len(list(reader.scan(layer_kind="files"))) == len(list(reader.scan(layer_kind="representations"))) == 3
        assert len(list(reader.scan(layer_kind="segments"))) == 6


def test_failed_initial_admission_is_not_cached_or_saved_as_task_progress(retained_experiment, monkeypatch):
    prepare, base = retained_experiment
    with prepare() as prepared:
        tasks = _tasks(prepared)
        opened = []
        open_reader = prepared._composition.catalog.open_reader

        def fail_once(reference):
            opened.append(reference)
            if len(opened) == 1:
                raise IntegrityError("fixture interrupted initial base admission")
            return open_reader(reference)

        monkeypatch.setattr(prepared._composition.catalog, "open_reader", fail_once)
        with pytest.raises(IntegrityError, match="initial base admission"):
            prepared.execute_task(prepared.handoff, tasks[0])
        assert prepared._composition.stores.latest(tasks[0].input_store.store_id) == tasks[0].input_store
        prepared.execute_task(prepared.handoff, tasks[0])
        prepared.execute_task(prepared.handoff, tasks[1])
        assert opened == [base, base]


@pytest.mark.parametrize("new_resource", [False, True])
def test_close_and_fresh_prepared_resources_readmit_before_unfinished_work(
    retained_experiment, monkeypatch, new_resource,
):
    prepare, base = retained_experiment
    with prepare() as first:
        tasks = _tasks(first)
        opened = _observe_admission(first, monkeypatch)
        first.execute_task(first.handoff, tasks[0])
        assert opened == [base]
        first.close()
        first.close()
        if new_resource:
            with prepare(handoff_ref=first.handoff_ref) as recovered:
                fresh_opens = _observe_admission(recovered, monkeypatch)
                recovered.execute_task(recovered.handoff, tasks[1])
                assert fresh_opens == [base] and opened == [base]
        else:
            first.execute_task(first.handoff, tasks[1])
            assert opened == [base, base]


@pytest.mark.parametrize("field", ["release_id", "locator", "digest"])
def test_cached_admission_is_bound_to_every_field_of_the_base_reference(retained_experiment, monkeypatch, field):
    prepare, base = retained_experiment
    with prepare() as prepared:
        tasks = _tasks(prepared)
        opened = _observe_admission(prepared, monkeypatch)
        prepared.execute_task(prepared.handoff, tasks[0])
        changed = sha256_digest(b"different result") if field == "digest" else "different-reference"
        values = {item.name: getattr(prepared.plan, item.name) for item in fields(prepared.plan) if item.name != "plan_id"}
        changed_plan = ProcessingPlan.create(**(values | {"base_release": replace(base, **{field: changed})}))
        store = prepared._composition.stores.load(tasks[1].input_store)
        with pytest.raises(IntegrityError, match="admitted reference"):
            prepared._composition.executor._reprocessing_reader(store, changed_plan)
        assert opened == [base]


@pytest.mark.parametrize("evidence", ["blob", "receipt", "record_member"])
def test_used_base_evidence_is_rechecked_after_admission(retained_experiment, monkeypatch, evidence):
    prepare, base = retained_experiment
    with prepare() as prepared:
        tasks = _tasks(prepared)
        opened = _observe_admission(prepared, monkeypatch)
        prepared.execute_task(prepared.handoff, tasks[0])
        store = prepared._composition.stores.load(tasks[1].input_store)
        entry, = store.entries
        reader = prepared._composition.executor._reprocessing_reader(store, prepared.plan)
        layer = "receipts" if evidence == "receipt" else "files"
        row, = reader.scan_source(layer_kind=layer, source_item_id=entry.source_item.item_id)
        if evidence == "blob":
            reference = BlobRef.from_dict(row["payload"]["blob"])
            path = prepared._composition.workspace.roots["blobStorage"] / reference.locator
        elif evidence == "receipt":
            reference = ArtifactRef.from_dict(row["payload"]["artifact"])
            path = prepared._composition.controls.root / reference.locator
        else:
            files, = (item for item in reader.release.active_layers if item.layer_kind == "files")
            records = prepared._composition.records
            root = json.loads((records.root / files.state_ref).read_text())
            member, = root["members"]  # This three-document fixture uses one record partition.
            path = records.root / member["path"]
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with pytest.raises(IntegrityError):
            prepared.execute_task(prepared.handoff, tasks[1])
        assert opened == [base]  # The used-byte check, not another full admission, refused it.


def test_run_finally_releases_the_reader_even_when_a_later_task_fails(retained_experiment, monkeypatch):
    prepare, _base = retained_experiment
    with prepare() as prepared:
        executor = prepared._composition.executor
        execute = executor.execute_store
        completed = []

        def fail_after_one(reference):
            if completed:
                raise RuntimeError("fixture stopped the next task")
            result = execute(reference)
            completed.append(result)
            return result

        monkeypatch.setattr(executor, "execute_store", fail_after_one)
        with pytest.raises(RuntimeError, match="stopped the next task"):
            prepared.run()
        assert completed and executor._base_reader is None
