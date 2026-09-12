"""A prepared worker admits base metadata once and keeps checking used evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
import json
from pathlib import Path
from threading import Barrier

import pytest

from docspec.application.base_reprocessing import prepare_base_reprocessing
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import EntryExecutionMode, StoreState
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


def test_reader_admits_each_physical_layer_once_and_fresh_reader_checks_again(retained_experiment, monkeypatch):
    prepare, base = retained_experiment
    with prepare() as prepared:
        records = prepared._composition.records
        admitted = []
        verify_members = records.verify_members

        def observed(reference):
            admitted.append(reference)
            verify_members(reference)

        monkeypatch.setattr(records, "verify_members", observed)
        reader = prepared._composition.catalog.open_reader(base)
        assert not admitted  # Metadata open remains cheap.
        expected = list(reader.scan(layer_kind="files"))
        with ThreadPoolExecutor(max_workers=3) as workers:
            found = list(workers.map(lambda row: list(reader.scan_source(
                layer_kind="files", source_item_id=row["sourceItemId"],
            )), expected))
        assert found == [[row] for row in expected]
        assert len(admitted) == 1
        fresh = prepared._composition.catalog.open_reader(base)
        assert list(fresh.scan(layer_kind="files")) == expected
        assert len(admitted) == 2 and admitted[0] == admitted[1]


def test_failed_physical_admission_remains_retryable(retained_experiment, monkeypatch):
    prepare, base = retained_experiment
    with prepare() as prepared:
        records = prepared._composition.records
        verify_members = records.verify_members
        attempts = []

        def fail_once(reference):
            attempts.append(reference)
            if len(attempts) == 1:
                raise IntegrityError("interrupted member admission")
            verify_members(reference)

        monkeypatch.setattr(records, "verify_members", fail_once)
        reader = prepared._composition.catalog.open_reader(base)
        with pytest.raises(IntegrityError, match="interrupted member admission"):
            list(reader.scan(layer_kind="files"))
        assert len(list(reader.scan(layer_kind="files"))) == 3
        assert len(attempts) == 2 and attempts[0] == attempts[1]


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


@pytest.mark.parametrize("evidence", ["blob", "receipt"])
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
        else:
            reference = ArtifactRef.from_dict(row["payload"]["artifact"])
            path = prepared._composition.controls.root / reference.locator
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with pytest.raises(IntegrityError):
            prepared.execute_task(prepared.handoff, tasks[1])
        assert opened == [base]  # The used-byte check, not another metadata admission, refused it.


def test_fresh_reader_and_full_audit_recheck_files_after_prior_admission(retained_experiment):
    prepare, base = retained_experiment
    with prepare() as prepared:
        catalog = prepared._composition.catalog
        records = prepared._composition.records
        reader = catalog.open_reader(base)
        assert len(list(reader.scan(layer_kind="files"))) == 3
        files = next(layer for layer in reader.release.active_layers if layer.layer_kind == "files")
        root = json.loads((records.root / files.state_ref).read_bytes())
        path = records.root / root["members"][0]["path"]
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        # An admitted reader does not freeze external files or repeat physical
        # hashes per query. A new lifetime and an explicit audit must recheck.
        with pytest.raises(IntegrityError):
            list(catalog.open_reader(base).scan(layer_kind="files"))
        with pytest.raises(IntegrityError):
            catalog.audit(base)


@pytest.mark.parametrize("corrupt", [False, True])
def test_public_inspection_releases_native_resources_on_close_or_audit_refusal(
    retained_experiment, monkeypatch, corrupt,
):
    prepare, base = retained_experiment
    with prepare() as prepared:
        composition = prepared._composition
        release = composition.catalog.open(base)
        plan = ProcessingPlan.from_dict(composition.controls.load(release.processing_plan))
        if corrupt:
            files = next(layer for layer in release.active_layers if layer.layer_kind == "files")
            root = json.loads((composition.records.root / files.state_ref).read_bytes())
            path = composition.records.root / root["members"][0]["path"]
            original = path.read_bytes()
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        closed = []
        close = type(composition.records).close

        def observed(storage):
            closed.append((storage, None if storage._scratch is None else Path(storage._scratch.name)))
            close(storage)

        monkeypatch.setattr(type(composition.records), "close", observed)

        def open_view():
            return open_local_inspection(plan, composition.workspace,
                document_release_producer=composition.catalog.producer, release_ref=base)

        if corrupt:
            with pytest.raises(IntegrityError):
                open_view()
        else:
            with open_view() as view:
                assert len(list(view.records("files"))) == 3
        assert len(closed) == 1
        storage, scratch = closed[0]
        assert storage._connection is None and storage._scratch is None
        assert scratch is not None and not scratch.exists()


def test_changed_reused_bytes_refuse_before_saving_the_prefix_checkpoint(retained_experiment, monkeypatch):
    prepare, _base = retained_experiment
    with prepare() as prepared:
        task = _tasks(prepared)[0]
        composition = prepared._composition
        planned = composition.stores.load(task.input_store)

        def corrupt_after_preparation(*args, **kwargs):
            seeded = prepare_base_reprocessing(*args, **kwargs)
            reference, = (captured.blob for captured in seeded.captured_files)
            path = composition.workspace.roots["blobStorage"] / reference.locator
            original = path.read_bytes()
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            return seeded

        monkeypatch.setattr("docspec.application.execution.prepare_base_reprocessing", corrupt_after_preparation)
        with pytest.raises(IntegrityError, match="blob bytes differ"):
            prepared.execute_task(prepared.handoff, task)
        latest = composition.stores.latest(task.input_store.store_id)
        assert latest is not None
        saved = composition.stores.load(latest)
        assert saved.state is StoreState.RUNNING
        assert saved.revision == planned.revision + 1  # Only the attempt start was persisted.
        assert saved.entries == planned.entries


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
