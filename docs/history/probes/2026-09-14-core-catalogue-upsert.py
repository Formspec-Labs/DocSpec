"""Bounded append/update qualification against existing imported catalogues.

Run with tools/with_iceberg.py from the directory containing both catalogues.
Creates explicitly labelled test branches; never changes the catalogue base.
"""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time

import pyarrow.parquet as pq

from docspec.domain.identity import decode_canonical_json_value, stable_urn
from docspec.errors import IntegrityError
from docspec.runtime import CoreWorkspace


source = Path(__file__).with_name("2026-09-14-iceberg-catalog-reimport.py")
spec = importlib.util.spec_from_file_location("reimport", source)
reimport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reimport)


def selected(workspace, state_id, keys):
    with workspace.publisher.session() as session, workspace.states.relation(session, state_id, scope=keys) as relation:
        return {key: None if payload is None else decode_canonical_json_value(payload)["value"]["value"]
                for key, payload in relation.project("member_key, occurrence_record").fetchall()}


def inventories(workspace, state_id):
    with workspace.publisher.session() as session:
        return {name: {path: {"rows": file.record_count, "bytes": file.file_size_in_bytes,
                             "mtime_ns": Path(path).stat().st_mtime_ns}
                       for path, file in reimport.inventory(layer)[0].items()}
                for name, layer in workspace.states.layers(session, state_id).items()}


def timed(call):
    start = time.perf_counter()
    result = call()
    return result, time.perf_counter() - start


def qualify(path):
    # The existing receipt supplies the qualified population; only a small
    # sample of existing values is needed to validate replacement semantics.
    receipt = json.loads((path / "receipt.json").read_text())
    count = receipt["source"]["rows"]
    with pq.ParquetFile(path / "expected.parquet") as expected:
        with closing(expected.iter_batches(batch_size=16, columns=["key"])) as batches:
            keys = next(batches).column("key").to_pylist()
    batch_id = "qualification-upsert-20260914-v1"
    timings = {}
    with CoreWorkspace(path / "workspace") as workspace:
        before = inventories(workspace, "catalogue")
        original = selected(workspace, "catalogue", keys)
        replacements = [(key, {**value, "docspecQualification": "replacement-only-test"}) for key, value in original.items()]
        added = [(f"urn:docspec:test-only:append:{i}", {"testOnly": True, "index": i, "title": "Synthetic append qualification"})
                 for i in range(1024)]
        rows = [*replacements, *added]
        assert all(value is None for value in selected(workspace, "catalogue", [key for key, _ in added]).values())
        # An isolated test pointer exercises promotion without adopting test
        # rows as the user's current real catalogue.
        workspace.maintenance.select_current(batch_id + ":initial", batch_id, ("state", "catalogue"), None)
        state, timings["upsert_1024_new_16_replacements"] = timed(
            lambda: workspace.upsert("catalogue", iter(rows), batch_id=batch_id, dataset=batch_id))
        comparison, timings["compare"] = timed(lambda: workspace.compare("catalogue", state.state_id, sample_limit=0))
        assert comparison["counts"] == {"added": 1024, "changed": 16, "removed": 0}
        assert selected(workspace, state.state_id, [key for key, _ in rows]) == dict(rows)
        assert selected(workspace, "catalogue", keys) == original
        after = inventories(workspace, state.state_id)
        assert all(all(after[name].get(path) == info for path, info in files.items()) for name, files in before.items())
        assert inventories(workspace, "catalogue") == before
        with workspace.publisher.session() as session:
            assert workspace.states.layers(session, state.state_id)["membership"].reference.record_count == count + 1024
        changed = [("urn:docspec:test-only:append:0", {"testOnly": True, "updatedAgain": True}),
                   ("urn:docspec:test-only:append:next", {"testOnly": True})]
        second, timings["next_batch_one_new_one_replacement"] = timed(
            lambda: workspace.upsert(state.state_id, changed, batch_id=batch_id + ":next", dataset=batch_id))
        assert workspace.compare(state.state_id, second.state_id, sample_limit=0)["counts"] == {"added": 1, "changed": 1, "removed": 0}
    with CoreWorkspace(path / "workspace") as workspace:
        retried, timings["retry_after_reopen"] = timed(
            lambda: workspace.upsert("catalogue", rows, batch_id=batch_id, dataset=batch_id))
        assert retried == state
        assert workspace.ledger.current(batch_id) == ("state", second.state_id)
        assert selected(workspace, second.state_id, [key for key, _ in changed]) == dict(changed)
        assert selected(workspace, "catalogue", keys) == original
        assert inventories(workspace, "catalogue") == before
        request_id = stable_urn("core-upsert", batch_id) + ":request"
        assert len([item for group in workspace.ledger.executions(request_id) for item in group]) == 1
        try:
            workspace.upsert("catalogue", rows[:-1], batch_id=batch_id, dataset=batch_id)
        except IntegrityError:
            pass
        else:
            raise AssertionError("changed retry input was accepted")
    return {"workspace": str(path / "workspace"), "base_rows": count, "first_state": state.state_id,
            "second_state": second.state_id, "timings_seconds": timings, "checks": {
                "add_replace_and_chained_batch": True, "exact_values": True, "base_files_unchanged_and_shared": True,
                "population": True, "reopen_and_exact_retry": True, "changed_retry_refused": True,
                "retry_does_not_rewind_current": True, "one_execution_for_retried_batch": True}}


def main():
    def timeout(*_):
        raise TimeoutError("180 second qualification bound")
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    root, output = map(Path, sys.argv[1:])
    report = {"settings": {"engine_threads": 1, "repetitions": 1, "time_bound_seconds": 180}, "catalogues": {}}
    try:
        for name in ("federal-register", "regulations-gov"):
            print("Qualifying " + name, flush=True)
            report["catalogues"][name] = qualify(root / name)
            print(json.dumps(report["catalogues"][name]), flush=True)
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")
        signal.alarm(0)


if __name__ == "__main__":
    main()
