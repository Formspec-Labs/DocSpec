"""Known-count checks for qualification observers, separate from capacity trials."""

from contextlib import closing
import hashlib
import json

from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.runtime import CoreWorkspace
from docspec.domain import core
from tests.support.core_runtime_experiment import (
    QueryProfiles, StorageSamples, allocation_observations, observations,
)


def test_inline_selection_reads_parent_files_once(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        workspace.create("root", ((str(index), {"x": index, "body": "x" * 1024}) for index in range(513)))
        with workspace.publisher.session() as session:
            parent = workspace.states.layers(session, "root")["entities"]
            parent_files = {member["path"].split("/")[-1] for member in parent._root["members"]}
            profiles = QueryProfiles(tmp_path / "profiles")
            definition = core.StateMembers(member_selector=core.JsonFields(selectors=(core.Field(label="x", pointer="/x"),)))
            with observations(workspace, {}, profiles):
                rows = list(workspace.selections._computed_rows(session, "root", definition))
            visits = [profile for profile in profiles.profiles if parent_files.intersection(profile["query_files"]) and profile["scans"]]
            assert len(visits) == 1
            assert len(rows) == 513
            assert {decode_canonical_json_value(row[2])[1][0][2] for row in rows} == set(range(513))


def test_codec_observers_count_exact_bytes_without_nested_double_charging(tmp_path):
    with CoreWorkspace(tmp_path / "first") as first, CoreWorkspace(tmp_path / "second") as second:
        outer, inner = {}, {}
        value = {"type": [1, "1", None]}
        expected = canonical_value_bytes(value)
        with observations(first, outer):
            assert canonical_value_bytes(value) == expected
            assert decode_canonical_json_value(expected) == value
            with observations(second, inner):
                assert canonical_value_bytes(value) == expected
                assert decode_canonical_json_value(expected) == value
            assert canonical_value_bytes(value) == expected
        for metrics, calls in ((outer, 2), (inner, 1)):
            assert metrics["canonical_encoder_calls"] == calls
            assert metrics["canonical_encoded_bytes"] == calls * len(expected)
            assert metrics["canonical_decoder_calls"] == 1
            assert metrics["canonical_decoded_bytes"] == len(expected)


def test_blob_observers_count_consumed_bytes_and_range_verification(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        reference = workspace.blobs.put_if_absent((b"abcdef",), media_type="application/octet-stream")
        metrics = {}
        with observations(workspace, metrics):
            with closing(workspace.blobs.read(reference, chunk_size=2)) as chunks:
                assert next(chunks) == b"ab"  # Closing a partial stream must stay partial.
            assert workspace.blobs.read_range(reference, start=1, end=4) == b"bcd"
        assert metrics["blob_reads_by_media_type"] == {
            "application/octet-stream": dict(stream_calls=1, returned_chunks=1, returned_bytes=2,
                range_calls=1, range_returned_bytes=3, verified_calls=1, verified_bytes=6),
        }


def test_storage_samples_separate_clean_scratch_and_new_durable_paths(tmp_path):
    trial = tmp_path / "trial"
    clean = tmp_path / "trial-clean"
    def write(root, name, size):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    write(trial, "workspace/existing.parquet", 2)
    samples = StorageSamples(trial)
    write(trial, "workspace/new.parquet", 3)
    write(trial, "workspace/ledger.sqlite-wal", 5)
    write(trial, "workspace/publication.lock", 7)
    write(trial, "workspace/.staging/pending", 11)
    write(trial, "scratch/spill", 13)
    write(clean, "workspace/new.parquet", 17)
    write(clean, "scratch/spill", 19)
    samples.sample()
    assert samples.peak == dict(storage_bytes=41, retained_bytes=17, scratch_bytes=24,
                                clean_storage_bytes=36, clean_retained_bytes=17, clean_scratch_bytes=19)
    assert samples.new_durable_files() == dict(new_durable_files=1, clean_new_durable_files=1)
    (trial / "scratch/spill").unlink()
    samples.sample()
    assert samples.peak["scratch_bytes"] == 24


def test_profiles_keep_query_identity_and_complete_file_union_without_receipt_sql(tmp_path):
    profiles = QueryProfiles(tmp_path)
    names = [f"{index:064x}.parquet" for index in range(8)]
    query = f"SELECT record_json FROM read_parquet({names!r})"
    path = tmp_path / "query.json"
    path.write_text(json.dumps({"query_name": query, "children": [{"operator_name": "READ_PARQUET",
        "extra_info": {"Filename(s)": f"{names[0]}, ...", "Projections": ["record_json"]}}]}))
    profiles.collect(path)
    profiles.collect(path)
    assert len(profiles.profiles) == 1
    observed = profiles.profiles[0]
    assert "query" not in observed
    assert observed["query_files"] == names
    assert observed["query_sha256"] == hashlib.sha256(query.encode()).hexdigest()
    assert json.loads(path.read_text())["query_name"] == query


def test_optional_allocations_report_net_live_blocks_not_gross_allocations():
    metrics = {}
    with allocation_observations(False, metrics):
        pass
    assert metrics == {}
    with allocation_observations(True, metrics):
        kept = [bytearray(1024) for _ in range(32)]
    assert len(kept) == 32
    assert metrics["peak_bytes"] >= metrics["current_bytes"] >= 32 * 1024
    assert metrics["net_live_allocation_count"] >= 32


def test_cli_receipt_records_cache_declaration_and_measurement_scope(tmp_path, monkeypatch, capsys):
    from tests.support import core_runtime_experiment as probe

    monkeypatch.setattr("sys.argv", ["probe", "generate", str(tmp_path), "--cache-description", "OS cache warm; new process"])
    monkeypatch.setattr(probe, "run_stage", lambda *args, **kwargs: {"verified": True})
    probe.main()
    receipt = json.loads((tmp_path / "generate-runtime.json").read_text())
    assert receipt["verified"] and receipt["cache_state_declared"]
    assert receipt["cache_description"] == "OS cache warm; new process"
    assert receipt["python_allocations"] is None
    assert "whole fresh process" in receipt["peak_rss_scope"]
    assert receipt["storage_file_changes"] == {"new_durable_files": 0, "clean_new_durable_files": 0}
    assert json.loads(capsys.readouterr().out) == receipt
