"""Bounded real-record import and retained-state comparison measurements."""

import cProfile
import hashlib
from importlib.metadata import version
import itertools
import json
from pathlib import Path
import pstats
import signal
import subprocess
import sys
import tempfile
import time

from docspec.domain.identity import canonical_value_bytes
from docspec.domain import core
from docspec.domain.core_admission import AdmittedRecord, encode_record
from docspec.runtime import CoreWorkspace


HERE = Path(__file__).resolve().parent
RETAINED = Path('/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-20260914')
CATALOGUES = {'federal-register': 'federal-register-2', 'regulations-gov': 'regulations-gov'}


def sample():
    rows, sources = [], []
    for name in CATALOGUES:
        receipt = json.loads((HERE / f'2026-09-14-iceberg-{name}-reimport.json').read_bytes())
        artifact = Path(receipt['source']['artifact'])
        root = json.loads((artifact / 'artifact.json').read_bytes())
        for descriptor in root['memberManifests']:
            manifest = json.loads((artifact / descriptor['objectKey']).read_bytes())
            for member in manifest['members']:
                if member['role'] != 'source-items':
                    continue
                path = artifact.parent / '.blobs/sha256' / member['blobRef'].split(':')[1]
                with path.open('rb') as stream:
                    raw = list(itertools.islice(stream, 32))
                sources.append({'path': str(path), 'rows': len(raw),
                                'sample_sha256': hashlib.sha256(b''.join(raw)).hexdigest()})
                for payload in raw:
                    key = name + ':' + json.loads(payload)['sourceItemId']
                    rows.append((key, payload.rstrip(b'\n')))
    return rows, sources


def digest(rows):
    result = hashlib.sha256()
    for key, payload in rows:
        result.update(canonical_value_bytes(key))
        result.update(payload)
    return result.hexdigest()


def imports(output):
    rows, output['sources'] = sample()
    rows.sort()
    output['rows'], expected = len(rows), digest(rows)
    output['input_sha256'] = expected
    output['runs'] = []
    for index in range(3):
        profiler = cProfile.Profile() if index == 2 else None
        with tempfile.TemporaryDirectory(prefix='core-import-check-') as temporary:
            started = time.perf_counter()
            if profiler:
                profiler.enable()
            with CoreWorkspace(temporary) as workspace:
                workspace.create('catalogue', ((key, json.loads(payload)) for key, payload in rows))
            if profiler:
                profiler.disable()
            elapsed = time.perf_counter() - started
            print(json.dumps({'import_complete': index, 'seconds': elapsed}), flush=True)
            with CoreWorkspace(temporary) as workspace:
                actual = digest((key, canonical_value_bytes(entity.value.value)) for key, entity in workspace.rows('catalogue'))
            assert actual == expected
        run = {'seconds': elapsed, 'profiled': profiler is not None, 'reopened_sha256': actual}
        if profiler:
            stats = pstats.Stats(profiler)
            run['profile'] = [{'function': f'{key[0]}:{key[1]}:{key[2]}', 'calls': value[1],
                               'self_seconds': value[2], 'cumulative_seconds': value[3]}
                              for key, value in sorted(stats.stats.items(), key=lambda item: -item[1][3])[:70]]
        output['runs'].append(run)
        print(json.dumps({'import_run': index, 'seconds': elapsed, 'profiled': profiler is not None}), flush=True)


def comparisons(output):
    output['runs'] = []
    for name, directory in CATALOGUES.items():
        with CoreWorkspace(RETAINED / directory / 'workspace') as workspace:
            # Historical controls timed compare after a revision had already
            # opened the native engine. Keep extension startup outside this timer.
            with workspace.records._cursor() as cursor:
                cursor.execute('SELECT 1')
            started = time.perf_counter()
            result = workspace.compare('catalogue', 'edit-1:state')
            elapsed = time.perf_counter() - started
        assert result['counts'] == {'added': 0, 'removed': 0, 'changed': 1}
        assert len(result['sample']) == 1 and result['sample'][0]['value_changed'] is True
        run = {'catalogue': name, 'seconds': elapsed, 'result': result}
        output['runs'].append(run)
        print(json.dumps(run), flush=True)


def snapshots(output):
    baseline = subprocess.check_output(['git', 'show', '0760e8b:src/docspec/domain/core_admission.py'], text=True)
    namespace = {'__name__': 'docspec.domain._baseline_admission'}
    exec(compile(baseline, 'baseline/core_admission.py', 'exec'), namespace)
    rows, _ = sample()
    payloads = [encode_record(core.Entity(format_version=1, entity_id=key, entity_type='occurrence',
                                         value=core.InlineValue(value=json.loads(payload))))
                for key, payload in rows[::16]]
    arms = {name: [kind(payload) for payload in payloads]
            for name, kind in [('before', namespace['AdmittedRecord']), ('after', AdmittedRecord)]}
    assert [record.value for record in arms['before']] == [record.value for record in arms['after']]
    output.update(rows=len(payloads), reads_per_row=3, runs=[])
    for name in ('before', 'after', 'after', 'before'):
        started = time.perf_counter()
        for record in arms[name]:
            for _ in range(3):
                record.value
        output['runs'].append({'arm': name, 'seconds': time.perf_counter() - started})
    print(json.dumps(output['runs']), flush=True)


if __name__ == '__main__':
    mode, destination = sys.argv[1:]
    def timeout(*_):
        raise TimeoutError('ten-minute measurement bound reached')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(600)
    output = {'mode': mode, 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'source_diff_sha256': hashlib.sha256(subprocess.check_output(['git', 'diff', '--', 'src'])).hexdigest(),
              'software': {name: version(name) for name in ('duckdb', 'pyarrow', 'msgspec', 'pyiceberg')},
              'engine_memory_bytes': 6 * 1024**3, 'engine_threads': 1, 'time_bound_seconds': 600,
              'verified': False}
    try:
        {'import': imports, 'compare': comparisons, 'snapshots': snapshots}[mode](output)
        output['verified'] = True
    except BaseException as error:
        output['error'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        Path(destination).write_text(json.dumps(output, indent=2) + '\n')
