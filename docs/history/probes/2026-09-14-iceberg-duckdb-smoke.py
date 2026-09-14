"""DuckDB Iceberg writer smoke test against Apache's disposable REST fixture.

Requires running Docker. Starts one loopback-only container and removes it on exit.
Keeps local data and a receipt in a temporary directory; changes no project dependencies.

uv run --no-project --python 3.12 --with 'duckdb==1.5.5' \
  --with 'pyiceberg==0.12.0' --with 'pyarrow==25.0.1' \
  python docs/history/probes/2026-09-14-iceberg-duckdb-smoke.py

Uses the earlier probe's exact rows and physical-inventory helpers. Writes use
DuckDB exclusively; PyIceberg independently checks current and historical rows.
This is a mechanism check, not a scale or latency benchmark.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from time import perf_counter, sleep
from urllib.error import URLError
from urllib.request import urlopen

import duckdb
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.table import StaticTable


FIXTURE = 'apache/iceberg-rest-fixture:1.10.1'
shared = runpy.run_path(str(Path(__file__).with_name('2026-09-14-iceberg-smoke.py')))
COUNT, SCHEMA, row = (shared[name] for name in ('COUNT', 'SCHEMA', 'row'))


def connect(endpoint):
    con = duckdb.connect()
    con.execute('INSTALL iceberg')
    con.execute('LOAD iceberg')
    con.execute(f"ATTACH '' AS ice (TYPE iceberg, ENDPOINT '{endpoint}', CLIENT_ID 'admin', CLIENT_SECRET 'password')")
    return con


def catalog(endpoint):
    return load_catalog('probe', type='rest', uri=endpoint, **{'py-io-impl': 'pyiceberg.io.pyarrow.PyArrowFileIO'})


def expected(stage):
    rows = [row(index) for index in range(COUNT)]
    if stage >= 1:
        rows[7] = {**rows[7], 'occurrence_id': 'changed-occurrence', 'value': b'changed'}
    if stage >= 2:
        del rows[9]
    return rows


def inventory(table):
    result = shared['inventory'](table, 'main')
    deletes = {f.file_path: {'rows': f.record_count, 'bytes': f.file_size_in_bytes, 'content': int(f.content)}
               for task in table.scan().plan_files() for f in task.delete_files}
    return {**result, 'delete_file_details': deletes}


def delta(before, after, seconds):
    new_deletes = after['delete_file_details'].keys() - before['delete_file_details'].keys()
    return {**shared['difference'](before, after, seconds),
            'new_delete_files': len(new_deletes),
            'new_delete_rows': sum(after['delete_file_details'][key]['rows'] for key in new_deletes),
            'new_delete_bytes': sum(after['delete_file_details'][key]['bytes'] for key in new_deletes)}


def check(con, endpoint, metadata):
    table = catalog(endpoint).load_table('probe.members')
    actual = con.execute('SELECT * FROM ice.probe.members ORDER BY member_key').to_arrow_table().to_pylist()
    assert actual == expected(2)
    assert shared['read'](table, 'main') == expected(2)
    for stage, location in enumerate(metadata):
        static = StaticTable.from_metadata(location)
        assert shared['read'](static, 'main') == expected(stage)
        actual = con.execute('SELECT * FROM iceberg_scan(?) ORDER BY member_key', [location]).to_arrow_table().to_pylist()
        assert actual == expected(stage)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--reopen':
        with connect(sys.argv[2]) as con:
            check(con, sys.argv[2], sys.argv[3:])
        print('Fresh-process DuckDB and PyIceberg current/historical reads passed')
        return
    # Colima shares the home directory by default, but not macOS /private/tmp.
    scratch = Path.home() / '.cache'
    scratch.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='docspec-iceberg-duckdb-', dir=scratch)).resolve()
    name = root.name
    command = ['docker', 'run', '--detach', '--name', name, '--user', '0',
               '--publish', '127.0.0.1::8181',
               '--volume', f'{root}:{root}', '--env', f'CATALOG_WAREHOUSE={root}/warehouse',
               '--env', f'CATALOG_URI=jdbc:sqlite:{root}/catalog.sqlite', FIXTURE]
    subprocess.run(command, check=True, capture_output=True, text=True)
    try:
        port = subprocess.check_output(['docker', 'port', name, '8181/tcp'], text=True).strip()
        endpoint = f'http://{port}'
        deadline = perf_counter() + 45
        while True:
            try:
                with urlopen(f'{endpoint}/v1/config', timeout=2) as response:
                    assert response.status == 200
                break
            except (URLError, ConnectionError):
                if perf_counter() >= deadline:
                    raise TimeoutError('REST fixture did not become ready') from None
                sleep(0.25)
        with connect(endpoint) as con:
            con.execute('CREATE SCHEMA ice.probe')
            con.execute("""CREATE TABLE ice.probe.members (member_key VARCHAR, occurrence_id VARCHAR, value BLOB)
                           WITH ('format-version'='2', 'write.update.mode'='merge-on-read',
                                 'write.delete.mode'='merge-on-read')""")
            for offset in (0, COUNT // 2):
                batch = pa.Table.from_pylist([row(i) for i in range(offset, offset + COUNT // 2)], schema=SCHEMA)
                con.register('input_rows', batch)
                con.execute('INSERT INTO ice.probe.members SELECT * FROM input_rows')
                con.unregister('input_rows')
            table = catalog(endpoint).load_table('probe.members')
            before = inventory(table)
            assert len(before['files']) == 2
            hashes = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in before['files']}
            metadata = [table.metadata_location]
            measurements = {}
            inventories = {'base': before}
            for label, sql in (
                ('one_row_update', "UPDATE ice.probe.members SET occurrence_id='changed-occurrence', value='changed'::BLOB WHERE member_key='key-00007'"),
                ('one_row_delete', "DELETE FROM ice.probe.members WHERE member_key='key-00009'"),
            ):
                started = perf_counter()
                con.execute(sql)
                seconds = perf_counter() - started
                table.refresh()
                after = inventory(table)
                measurements[label] = delta(before, after, seconds)
                inventories[label] = after
                metadata.append(table.metadata_location)
                before = after
            check(con, endpoint, metadata)
            extension = con.execute("SELECT extension_version FROM duckdb_extensions() WHERE extension_name='iceberg'").fetchone()[0]
        subprocess.run([sys.executable, __file__, '--reopen', endpoint, *metadata], check=True)
        assert hashes == {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in hashes}
        assert measurements['one_row_update']['new_data_rows'] == 1
        assert measurements['one_row_delete']['new_data_rows'] == 0
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', FIXTURE], text=True))[0]
        result = {
            'versions': {name: importlib.metadata.version(name) for name in ('duckdb', 'pyiceberg', 'pyarrow')},
            'iceberg_extension': extension, 'rest_fixture': FIXTURE, 'rest_fixture_digest': image['RepoDigests'],
            'rows': COUNT, 'base_data_files': 2, **measurements,
            'exact_values_both_readers': True, 'fresh_process_reopen': True,
            'catalog_free_historical_reads_both_readers': True, 'original_files_byte_unchanged': True,
            'inventories': inventories, 'metadata': metadata, 'workspace': str(root),
            'limitations': ['Sequential main-branch writes; no branch-targeted writes',
                            'No scale, compaction, concurrency, or DocSpec publication/crash qualification',
                            'Operation seconds exclude inventory, startup and setup; not comparable latency benchmarks'],
        }
        (root / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True, check=False)
        (root / 'catalog.log').write_text(logs.stdout + logs.stderr)
        subprocess.run(['docker', 'rm', '--force', name], check=True, capture_output=True)


if __name__ == '__main__':
    main()
