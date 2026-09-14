"""Independent clean storage and checkpoint-plus-suffix production probe paths."""

import json
from pathlib import Path
import subprocess
import sys

from tests.support.core_runtime_experiment import (
    audit, checkpoint_history_suffix, clean, clean_directory, control_workspace, run_stage,
)


def test_clean_control_survives_without_original_and_compares_after_fresh_reopen(tmp_path):
    directory = tmp_path / "trial"
    run_stage(directory, "generate", count=8)
    run_stage(directory, "build")
    run_stage(directory, "history", history_length=11)
    state = "history:10"
    with control_workspace(directory) as original:
        built = clean(original, state, 8, 1024)
        original_ids = [entity.entity_id for _, entity in original.rows(state)]
    assert Path(built["clean_directory"]) == tmp_path / "trial-clean"
    assert built["separate_storage_allowance_bytes"] == 80 * 1024**3
    original_path = directory / "workspace"
    unavailable = directory / "workspace-unavailable"
    original_path.rename(unavailable)
    try:
        with control_workspace(clean_directory(directory)) as control:
            assert audit(control, state, 8, 1024)["members"] == 8
            assert [entity.entity_id for _, entity in control.rows(state)] == original_ids
    finally:
        unavailable.rename(original_path)
    script = "from tests.support.core_runtime_experiment import compare_clean; import json,sys; print(json.dumps(compare_clean(sys.argv[1], 'history:10', 8, 1024)))"
    result = subprocess.run([sys.executable, "-c", script, str(directory)], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    compared = json.loads(result.stdout)
    assert compared["compared_members"] == 8 and compared["occurrence_identities_equal"]
    assert compared["counts"] == {"added": 0, "removed": 0, "changed": 0}


def test_checkpointed_history_reopens_and_continues_at_the_next_revision_index(tmp_path):
    directory = tmp_path / "trial"
    run_stage(directory, "generate", count=4)
    run_stage(directory, "build")
    run_stage(directory, "history", history_length=5)
    result = checkpoint_history_suffix(directory, history_length=5, suffix_length=6, count=4)
    assert result["state_id"] == "history:10"
    assert result["start_index"] == 5 and result["revision_count"] == 6
    assert result["checkpoint_state_id"] == "history:4" and result["original_history_preserved"]
    assert result["recovered_audit"]["members"] == 4
    assert set(result["phases"]) == {"before_checkpoint_recovery_seconds", "checkpoint_seconds",
        "after_checkpoint_recovery_seconds", "suffix_creation_seconds", "suffix_recovery_seconds"}
    with control_workspace(directory) as workspace:
        final = dict((key, entity.value.value["title"]) for key, entity in workspace.rows("history:10"))
        assert final == {"0000000": "history-8", "0000001": "history-9", "0000002": "history-10", "0000003": "history-7"}
        revision = next(workspace.ledger.read_records([("revision", "history:5:revision")]))[0].value
        assert revision.base_state_id == "history:4"
