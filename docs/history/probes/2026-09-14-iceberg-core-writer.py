"""Bounded production-path measurement; requires the configured local REST catalog.

Run from the checkout: uv run python tools/with_iceberg.py python
  docs/history/probes/2026-09-14-iceberg-core-writer.py ./probe-workspace ./receipt.json
"""

import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

from docspec.domain import core
from docspec.runtime import CoreWorkspace


def main():
    root, output = map(Path, sys.argv[1:3])
    count = 100_000
    measurements = {}
    with CoreWorkspace(root) as workspace:
        start = perf_counter()
        workspace.create('root', ((f'{index:08d}', {'n': index, 'body': 'x' * 256}) for index in range(count)))
        measurements['create_seconds'] = perf_counter() - start
        with workspace.publisher.session() as session:
            base = workspace.states.layers(session, 'root')['membership']
            data = {task.file.file_path: task.file for task in base.table.scan().plan_files()}
            hashes = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in data}
        start = perf_counter()
        workspace.revise(core.Revision(format_version=1, revision_id='remove-one', base_state_id='root', result_state_id='changed',
                                       edits=(core.Remove(sequence=0, member_key='00000042'),)))
        measurements['remove_one_seconds'] = perf_counter() - start
        with workspace.publisher.session() as session:
            changed = workspace.states.layers(session, 'changed')['membership']
            tasks = list(changed.table.scan().plan_files())
            after = {task.file.file_path: task.file for task in tasks}
            deletes = {delete.file_path: delete for task in tasks for delete in task.delete_files}
            assert after.keys() == data.keys()
            assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
            assert changed.reference.record_count == count - 1
            measurements.update(base_data_files=len(data), new_data_files=len(after.keys() - data.keys()),
                new_data_rows=sum(file.record_count for path, file in after.items() if path not in data),
                delete_files=len(deletes), delete_rows=sum(file.record_count for file in deletes.values()),
                delete_bytes=sum(file.file_size_in_bytes for file in deletes.values()),
                base_root_bytes=(workspace.records.root / base.reference.state_ref).stat().st_size,
                revised_root_bytes=(workspace.records.root / changed.reference.state_ref).stat().st_size)
        start = perf_counter()
        comparison = workspace.compare('root', 'changed')
        measurements['compare_seconds'] = perf_counter() - start
        assert comparison['counts'] == {'added': 0, 'removed': 1, 'changed': 0}
    with CoreWorkspace(root) as workspace:
        with workspace.publisher.session() as session:
            with workspace.states.relation(session, 'root') as relation:
                assert relation.aggregate('count(*)').fetchone() == (count,)
            with workspace.states.relation(session, 'changed') as relation:
                assert relation.aggregate('count(*)').fetchone() == (count - 1,)
    import duckdb
    import pyiceberg
    receipt = {'rows': count, 'duckdb': duckdb.__version__, 'pyiceberg': pyiceberg.__version__,
               'measurements': measurements, 'verified': True,
               'limits': 'One local run, compressible 256-byte bodies, warm filesystem, one native thread. Measures production create and removal, not concurrent load or whole-value selection throughput.'}
    output.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
