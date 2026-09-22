"""Small complete recipe checks do not make a larger-than-memory capacity claim."""

import json
from pathlib import Path
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

from tests.support.core_bulk_experiment import generate
from tests.support.core_larger_than_memory import derive, file_digest


def test_two_copy_fresh_process_recipe_preserves_values_and_checks_complete_outputs(tmp_path):
    """Four fresh subprocess stages copy the source, keep it byte-identical and verify all 32 outputs."""
    source = tmp_path / "source"
    generate(source, 16)
    fixture = source / "base.parquet"
    original = fixture.read_bytes()
    trial = tmp_path / "trial"
    for stage in ("derive", "build", "select", "verify"):
        run = subprocess.run([sys.executable, "-m", "tests.support.core_larger_than_memory", stage, str(trial),
            "--source", str(fixture), "--source-sha256", file_digest(fixture), "--count", "16"],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=45)
        assert run.returncode == 0, json.loads(run.stdout).get("error", run.stderr)
        receipt = json.loads((trial / (stage + ".json")).read_text())
        assert receipt["sources_unchanged_during_run"] and receipt["peak_rss_bytes"] > 0
        assert receipt["sampled_peak"]["total_trial_bytes"] > 0
        assert receipt["process_target_bytes"] == (20 if stage == "build" else 12) * 1024**3
        if stage == "derive":
            assert receipt["build_process_target_bytes"] == 20 * 1024**3
    assert fixture.read_bytes() == original
    originals = {row["member_key"]: row for row in pq.read_table(fixture).to_pylist()}
    copied = pq.read_table(trial / "base.parquet").to_pylist()
    assert len({row["member_key"] for row in copied}) == len({row["occurrence_id"] for row in copied}) == 32
    for row in copied:
        copy, key = row["member_key"].split(":")
        assert row["payload"] == originals[key]["payload"]
        assert row["occurrence_id"] == f"urn:docspec:two-copy:{copy}:" + originals[key]["occurrence_id"]
    selected = json.loads((trial / "select.json").read_text())
    checked = json.loads((trial / "verify.json").read_text())
    assert checked["checked_rows"] == 32 and checked["evidence"] == selected["evidence"]
    assert selected["evidence"]["byte_size"] > 32 * 8192
    assert not selected["larger_than_process_target"]


def test_derivation_refuses_an_unpinned_source_without_creating_a_fixture(tmp_path):
    """A source whose digest does not match the supplied pin refuses before creating a fixture."""
    source = tmp_path / "source"
    generate(source, 2)
    trial = tmp_path / "trial"
    trial.mkdir()
    with pytest.raises(ValueError, match="supplied pin"):
        derive(trial, source / "base.parquet", "0" * 64, 2)
    assert not (trial / "base.parquet").exists()


@pytest.mark.parametrize("replace_missing", [False, True])
def test_oracle_rejects_missing_keys_even_when_a_valid_duplicate_preserves_count(tmp_path, monkeypatch, replace_missing):
    """The oracle still reports a duplicate key or incomplete population when the row count is preserved."""
    from contextlib import closing

    from docspec.runtime import CoreWorkspace
    from tests.support.core_larger_than_memory import run_stage, verify

    source, trial = tmp_path / "source", tmp_path / "trial"
    generate(source, 2)
    trial.mkdir()
    derive(trial, source / "base.parquet", file_digest(source / "base.parquet"), 2)
    run_stage(trial, "build")
    run_stage(trial, "select")
    with CoreWorkspace(trial / "workspace") as workspace:
        actual_rows = workspace.selections.rows
        def corrupted(session, record):
            with closing(actual_rows(session, record)) as rows:
                first = next(rows)
                yield first
                remaining = list(rows)
                yield from remaining[:-1]
                if replace_missing:
                    yield first
        monkeypatch.setattr(workspace.selections, "rows", corrupted)
        with pytest.raises(AssertionError, match="duplicate copied member key" if replace_missing else "population is incomplete"):
            verify(workspace, 2)
