"""One complete retained-catalogue import and comparison through Core's APIs."""

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import time
import traceback

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pyiceberg

from docspec.domain import core
from docspec.application.core_edits import prepare_value_edit
from docspec.domain.identity import canonical_value_bytes
from docspec.ports.record_storage import bounded_batches
from docspec.runtime import CoreWorkspace


DIGEST_SCHEMA = pa.schema([("key", pa.string()), ("digest", pa.binary(32))])


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def emit(stage, **values):
    print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "stage": stage, **values}), flush=True)


def check_space(path):
    free = shutil.disk_usage(path).free
    if free < 10 * 1024**3:
        raise RuntimeError(f"disk stop bound reached: {free} bytes free")


def inventory(layer):
    tasks = list(layer.table.scan().plan_files())
    data = {task.file.file_path: task.file for task in tasks}
    deletes = {file.file_path: file for task in tasks for file in task.delete_files}
    return data, deletes


def write_digests(writer, rows):
    if rows:
        writer.write_table(pa.Table.from_pylist(rows, schema=DIGEST_SCHEMA))
        rows.clear()


def run(artifact, output, receipt, minutes):
    root_bytes = (artifact / "artifact.json").read_bytes()
    root = json.loads(root_bytes)
    manifests, source_members = [], []
    for descriptor in root["memberManifests"]:
        path = artifact / descriptor["objectKey"]
        assert path.stat().st_size == descriptor["byteSize"]
        assert "sha256:" + sha(path) == descriptor["sha256"]
        manifest = json.loads(path.read_bytes())
        assert len(manifest["members"]) == descriptor["memberCount"]
        manifests.append(descriptor)
        for member in manifest["members"]:
            path = (artifact.parent / ".blobs/sha256" / member["blobRef"].split(":")[1]
                    if "blobRef" in member else artifact / member["objectKey"])
            assert path.stat().st_size == member["byteSize"]
            if member["role"] == "source-items":
                source_members.append((member, path))
            else:
                assert "sha256:" + sha(path) == member.get("blobRef", member.get("sha256"))
    count = sum(member["recordCount"] for member, _ in source_members)
    source_bytes = sum(member["byteSize"] for member, _ in source_members)
    receipt.update(source={"artifact": str(artifact), "artifact_digest": root["artifactDigest"],
                           "logical_id": root["logicalId"], "root_sha256": hashlib.sha256(root_bytes).hexdigest(),
                           "manifests": manifests, "rows": count, "source_bytes": source_bytes},
                   software={"python": sys.version, "duckdb": duckdb.__version__,
                             "pyiceberg": pyiceberg.__version__, "pyarrow": pa.__version__,
                             "commit": subprocess.check_output(["git", "-C", str(Path(__file__).resolve().parents[3]),
                                                                "rev-parse", "HEAD"], text=True).strip()},
                   settings={"engine_memory_bytes": 6 * 1024**3, "engine_threads": 1,
                             "repetitions": 1, "time_bound_seconds": minutes * 60},
                   checks={}, timings={})
    save(output, receipt)
    expected_path, actual_path = output / "expected.parquet", output / "actual.parquet"
    sample, dispositions = [], Counter()
    start = time.perf_counter()
    emit("import", rows=count, source_bytes=source_bytes)

    def rows():
        seen = 0
        with pq.ParquetWriter(expected_path, DIGEST_SCHEMA, compression="zstd") as expected:
            pending = []
            for member, path in source_members:
                digest, size, partition_count = hashlib.sha256(), 0, 0
                with path.open("rb") as stream:
                    for raw in stream:
                        assert raw.endswith(b"\n"), "source row is missing its newline"
                        digest.update(raw)
                        size += len(raw)
                        value = json.loads(raw)
                        key = value["sourceItemId"]
                        pending.append({"key": key, "digest": hashlib.sha256(raw[:-1]).digest()})
                        if partition_count < 16:
                            sample.append(key)
                        dispositions[value["selection"]["disposition"]] += 1
                        partition_count += 1
                        seen += 1
                        if len(pending) == 8192:
                            write_digests(expected, pending)
                            check_space(output)
                        if seen % 50_000 == 0:
                            emit("source_rows", rows=seen, elapsed_seconds=time.perf_counter() - start)
                        yield key, value
                assert size == member["byteSize"]
                assert partition_count == member["recordCount"]
                assert "sha256:" + digest.hexdigest() == member["blobRef"]
            write_digests(expected, pending)
        assert seen == count

    with CoreWorkspace(output / "workspace") as workspace:
        workspace.create("catalogue", rows())
    receipt["timings"]["import_seconds"] = time.perf_counter() - start
    receipt["checks"]["source_member_hashes"] = True
    receipt["source"]["dispositions"] = dict(dispositions)
    save(output, receipt)
    emit("import_complete", seconds=receipt["timings"]["import_seconds"])

    start = time.perf_counter()
    with CoreWorkspace(output / "workspace") as workspace, workspace.publisher.session() as session:
        layers = workspace.states.layers(session, "catalogue")
        for layer in layers.values():
            workspace.records.verify_members(layer.reference)
        receipt["checks"]["retained_file_hashes"] = True
        receipt["storage"] = {
            name: {"data_files": len(inventory(layer)[0]),
                   "data_bytes": sum(file.file_size_in_bytes for file in inventory(layer)[0].values()),
                   "root_bytes": (workspace.records.root / layer.reference.state_ref).stat().st_size}
            for name, layer in layers.items()
        }
        emit("compare_all_values", rows=count)
        checked = 0
        with pq.ParquetWriter(actual_path, DIGEST_SCHEMA, compression="zstd") as actual:
            with workspace.states.relation(session, "catalogue") as relation:
                with closing(relation.to_arrow_reader(256)) as reader:
                    pending = []
                    for batch in bounded_batches(reader, byte_column="occurrence_record"):
                        for row in batch.to_pylist():
                            entity = json.loads(row["occurrence_record"])
                            assert entity["entity_id"] == row["occurrence_id"]
                            assert entity["value"]["kind"] == "inline"
                            value = entity["value"]["value"]
                            assert value["sourceItemId"] == row["member_key"]
                            pending.append({"key": row["member_key"],
                                            "digest": hashlib.sha256(canonical_value_bytes(value)).digest()})
                            checked += 1
                        if len(pending) >= 8192:
                            write_digests(actual, pending)
                            check_space(output)
                        if checked % 128_000 == 0:
                            emit("compared_rows", rows=checked)
                    write_digests(actual, pending)
        assert checked == count
    with duckdb.connect(config={"memory_limit": "512MB", "threads": "1"}) as check:
        check.from_parquet(str(expected_path)).create_view("expected")
        check.from_parquet(str(actual_path)).create_view("actual")
        for table in ("expected", "actual"):
            assert check.sql(f'SELECT count(*), count(DISTINCT key) FROM {table}').fetchone() == (count, count)
        mismatch = check.sql('SELECT count(*) FROM expected FULL OUTER JOIN actual USING (key) '
                             'WHERE expected.digest IS DISTINCT FROM actual.digest').fetchone()[0]
        assert mismatch == 0, f"{mismatch} values differ from the retained source"
    receipt["checks"].update(reopened_rows=checked, value_mismatches=mismatch, distinct_keys=count)
    receipt["timings"]["verify_seconds"] = time.perf_counter() - start
    save(output, receipt)
    emit("comparison_complete", rows=checked, mismatches=mismatch)

    with CoreWorkspace(output / "workspace") as workspace:
        with workspace.publisher.session() as session:
            base = workspace.states.layers(session, "catalogue")
            original = {name: inventory(layer)[0] for name, layer in base.items()}
            originals = {path: sha(Path(path)) for files in original.values() for path in files}
            with workspace.states.relation(session, "catalogue", scope=[sample[0]]) as relation:
                occurrence = relation.project("occurrence_id").fetchone()[0]
        assert len(sample) >= 1024
        revisions = [("remove-1", (core.Remove(sequence=0, member_key=sample[0]),)),
                     ("remove-1024", tuple(core.Remove(sequence=i, member_key=key)
                                           for i, key in enumerate(sample[:1024]))),
                     ("edit-1", ())]
        receipt["revisions"] = {}
        for name, edits in revisions:
            emit("revision", name=name)
            start = time.perf_counter()
            value_edits = ()
            if name == "edit-1":
                with workspace.publisher.session() as session:
                    prepared, evidence = prepare_value_edit(workspace.operations, occurrence,
                        [{"op": "add", "path": "/_reimportCheck", "value": True}], session=session)
                    workspace.operations.publish([prepared], session=session)
                value_edits = (evidence,)
                edits = (core.Put(sequence=0, member_key=sample[0], occurrence_id=evidence.result_occurrence_id),)
            workspace.revise(core.Revision(format_version=1, revision_id=name, base_state_id="catalogue",
                                           result_state_id=name + ":state", edits=edits, value_edits=value_edits))
            result = {"publish_seconds": time.perf_counter() - start, "edit_count": len(edits), "layers": {}}
            with workspace.publisher.session() as session:
                changed = workspace.states.layers(session, name + ":state")
                for kind, layer in changed.items():
                    data, deletes = inventory(layer)
                    assert original[kind].keys() <= data.keys(), "revision replaced original data files"
                    new_files = data.keys() - original[kind].keys()
                    result["layers"][kind] = {
                        "new_data_files": len(new_files), "new_data_rows": sum(data[p].record_count for p in new_files),
                        "new_data_bytes": sum(data[p].file_size_in_bytes for p in new_files),
                        "delete_files": len(deletes), "delete_rows": sum(f.record_count for f in deletes.values()),
                        "delete_bytes": sum(f.file_size_in_bytes for f in deletes.values()),
                    }
                assert result["layers"]["entities"]["new_data_rows"] == (1 if value_edits else 0)
                assert result["layers"]["membership"]["delete_rows"] == len(edits)
                assert result["layers"]["membership"]["new_data_rows"] == (1 if value_edits else 0)
            start = time.perf_counter()
            difference = workspace.compare("catalogue", name + ":state")
            assert difference["counts"] == {"added": 0, "removed": 0 if value_edits else len(edits),
                                             "changed": 1 if value_edits else 0}
            result.update(compare_seconds=time.perf_counter() - start, counts=difference["counts"])
            receipt["revisions"][name] = result
            save(output, receipt)
            emit("revision_complete", name=name, **result)
        assert all(sha(Path(path)) == digest for path, digest in originals.items())
    receipt["checks"]["original_data_files_unchanged"] = True
    receipt["storage"]["workspace_bytes"] = sum(p.stat().st_size for p in (output / "workspace").rglob("*") if p.is_file())
    receipt["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    receipt["verified"] = True
    receipt["limits"] = "One local run per catalogue; one native thread. Import includes input hashing and expected digest output. No equivalent old-backend timing, publisher downloads, concurrent load, or full-population selection benchmark."


def save(output, receipt):
    temporary = output / "receipt.tmp"
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.replace(output / "receipt.json")


def main():
    artifact, output = (Path(p).resolve() for p in sys.argv[1:3])
    minutes = int(sys.argv[3]) if len(sys.argv) > 3 else 45
    if minutes <= 0:
        raise ValueError("time bound must be positive")
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"verified": False, "output": str(output)}
    def timeout(*_):
        raise TimeoutError(f"{minutes}-minute dataset stop bound reached")
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(minutes * 60)
    try:
        check_space(output)
        run(artifact, output, receipt, minutes)
    except BaseException as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        raise
    finally:
        save(output, receipt)
        emit("finished", verified=receipt["verified"], receipt=str(output / "receipt.json"))


if __name__ == "__main__":
    main()
