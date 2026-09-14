"""Server-free Iceberg smoke test; no DocSpec runtime dependency changes.

uv run --no-project --python 3.12 --with 'pyiceberg[sql-sqlite]==0.12.0' \
  --with 'pyarrow==25.0.1' python docs/history/probes/2026-09-14-iceberg-smoke.py

Tests branch isolation, exact rows, fresh-process reopening, catalog-free reads,
protected snapshots, shared manifests, and physical work for single-row edits.
Operation timings include post-write inventory and exclude interpreter startup.
This is not a capacity benchmark or a DocSpec replacement adapter.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter

import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.table import StaticTable


COUNT = 4096
SCHEMA = pa.schema([('member_key', pa.string()), ('occurrence_id', pa.string()), ('value', pa.binary())])


def row(index):
    return {'member_key': f'key-{index:05}', 'occurrence_id': f'occurrence-{index:05}',
            'value': hashlib.sha256(f'value-{index}'.encode()).digest()}


def catalog(root):
    return load_catalog('probe', type='sql', uri=f'sqlite:///{root / "catalog.sqlite"}',
                        warehouse=(root / 'warehouse').as_uri())


def read(table, name):
    snapshot = table.refs()[name].snapshot_id
    return sorted(table.scan(snapshot_id=snapshot).to_arrow().to_pylist(), key=lambda item: item['member_key'])


def check(table):
    expected = [row(index) for index in range(COUNT)]
    assert read(table, 'main') == expected
    assert read(table, 'retained-base') == expected
    left = list(expected)
    left[7] = {**left[7], 'occurrence_id': 'changed-occurrence', 'value': b'changed'}
    assert read(table, 'left') == left
    assert read(table, 'right') == expected[:9] + expected[10:]


def inventory(table, name):
    snapshot_id = table.refs()[name].snapshot_id
    tasks = list(table.scan(snapshot_id=snapshot_id).plan_files())
    return {
        'snapshot_id': snapshot_id,
        'files': {task.file.file_path: {'rows': task.file.record_count, 'bytes': task.file.file_size_in_bytes}
                  for task in tasks},
        'delete_files': len({file.file_path for task in tasks for file in task.delete_files}),
        'manifests': sorted(manifest.manifest_path for manifest in table.snapshot_by_id(snapshot_id).manifests(table.io)),
    }


def difference(before, after, seconds):
    added = after['files'].keys() - before['files'].keys()
    return {
        'seconds': seconds,
        'new_data_files': len(added),
        'new_data_rows': sum(after['files'][key]['rows'] for key in added),
        'new_data_bytes': sum(after['files'][key]['bytes'] for key in added),
        'reused_data_files': len(before['files'].keys() & after['files'].keys()),
        'removed_data_files_from_branch': len(before['files'].keys() - after['files'].keys()),
        'delete_files': after['delete_files'],
        'reused_manifests': len(set(before['manifests']) & set(after['manifests'])),
    }


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--reopen':
        root = Path(sys.argv[2])
        check(catalog(root).load_table('probe.members'))
        check(StaticTable.from_metadata(sys.argv[3]))
        print('Fresh-process catalog and static-metadata reads passed')
        return
    root = Path(tempfile.mkdtemp(prefix='docspec-iceberg-smoke-'))
    cat = catalog(root)
    cat.create_namespace('probe')
    table = cat.create_table('probe.members', schema=SCHEMA)
    # Two deliberate files make sharing vs rewriting observable.
    for offset in (0, COUNT // 2):
        table.append(pa.Table.from_pylist([row(i) for i in range(offset, offset + COUNT // 2)], schema=SCHEMA))
    base = table.current_snapshot().snapshot_id
    with table.manage_snapshots() as snapshots:
        snapshots.create_branch(base, 'left')
        snapshots.create_branch(base, 'right')
        snapshots.create_tag(base, 'retained-base')
    before = inventory(table, 'main')
    start = perf_counter()
    table.upsert(pa.Table.from_pylist([{**row(7), 'occurrence_id': 'changed-occurrence', 'value': b'changed'}], schema=SCHEMA),
                 join_cols=['member_key'], branch='left')
    update = difference(before, inventory(table, 'left'), perf_counter() - start)
    start = perf_counter()
    table.delete("member_key == 'key-00009'", branch='right')
    deletion = difference(before, inventory(table, 'right'), perf_counter() - start)
    check(table)
    try:
        table.maintenance.expire_snapshots().by_id(base).commit()
    except ValueError as error:
        protection = str(error)
    else:
        raise AssertionError('referenced base snapshot was not protected')
    subprocess.run([sys.executable, __file__, '--reopen', str(root), table.metadata_location], check=True)
    result = {
        'versions': {name: importlib.metadata.version(name) for name in ('pyiceberg', 'pyarrow', 'sqlalchemy')},
        'rows': COUNT, 'base_data_files': len(before['files']), 'one_row_update': update, 'one_row_delete': deletion,
        'exact_branch_values': True, 'fresh_process_reopen': True, 'catalog_free_read': True,
        'snapshot_expiration_refused': protection, 'workspace': str(root),
        'limitations': ['No DuckDB Iceberg writer or REST catalog', 'SQLite catalog is exploratory',
                        'No portable file relocation, DocSpec admission/crash boundary, or scale qualification'],
    }
    (root / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
