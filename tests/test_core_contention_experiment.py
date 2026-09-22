"""Small inputs exercise the actual multi-process capacity operations."""

from tests.support.core_contention_experiment import cleanup_race, contention
from tests.support.core_runtime_experiment import run_stage


def test_writer_changes_real_members_while_four_readers_reconcile(tmp_path):
    """Four reader processes reconcile two batches while the writer's stale updates are all refused."""
    run_stage(tmp_path, "generate", count=8)
    run_stage(tmp_path, "build")
    result = contention(tmp_path, batches=2, changed_members=2, timeout=60)
    assert result["acknowledged_batches_reconciled"] == 2 and result["reconciled_value_edits"] == 4
    assert result["changed_members_per_batch"] == 2 and result["process_count"] == 5
    writer = next(worker for worker in result["workers"] if worker["role"] == "writer")
    assert writer["changed_members"] == 4
    assert all(batch["stale_update_refused"] for batch in writer["batches"])
    readers = [worker for worker in result["workers"] if worker["role"].startswith("reader:")]
    assert len(readers) == 4 and all(worker["named_seconds"] for worker in readers)


def test_real_process_cleanup_race_preserves_content_and_recovers_removal(tmp_path):
    """Publication during cleanup refuses, in-flight cleanup refuses, and an interrupted removal is recovered."""
    result = cleanup_race(tmp_path, timeout=60)
    assert len({worker["pid"] for worker in result["workers"]}) == 2
    publisher = next(worker for worker in result["workers"] if worker["role"] == "publication")
    cleanup = next(worker for worker in result["workers"] if worker["role"] == "cleanup")
    assert publisher["publication_during_cleanup_refused"]
    assert cleanup["inflight_cleanup_refused"] and cleanup["retained_shared_and_new_survive"]
    assert cleanup["interrupted_removal_recovered"] == {"absent": 1}
