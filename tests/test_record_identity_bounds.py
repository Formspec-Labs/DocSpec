"""Record identity-bound contract: Iceberg's own indexes preserve exact keys (including empty, unicode,
10,000-byte and NUL identities) in ordered and unordered writes, and DocSpec keeps no parallel index.

Covers native lookup distinguishing combining-mark from precomposed values, disjoint union keeping base files
while checking overlap, and exclude_existing filtering an overlapping delta.
"""

from contextlib import closing

import pytest

from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.storage.records import IcebergRecordStorage
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError
from tests.support.iceberg_records import files

SCHEMA = RecordSchema('bounds/1', ('id', 'group', 'value'), 'id', 'group')
POLICY = PartitionPolicy('single', 1)


def layer(storage, identities, *, ordered=True):
    """Retain one layer holding the given identities, sorted unless the write is declared unordered."""
    return storage.retain_batches(
        encoded_batches(((key, 'same', canonical_json_bytes({'id': key, 'group': 'same', 'value': key}))
                         for key in sorted(identities, reverse=not ordered)), ENCODED_RECORD_SCHEMA, byte_column=2),
        layer_kind='bounds', schema=SCHEMA, partition_policy=POLICY, ordered=ordered)


@pytest.mark.parametrize('ordered', [True, False])
def test_writer_bounds_preserve_long_unicode_identities_and_native_lookup(tmp_path, ordered):
    identities = ['', '\0', 'A', 'a', 'e\u0301', 'é', '中', '\uffff', '😀', 'z' * 10_000]
    with closing(IcebergRecordStorage(tmp_path)) as storage:
        admitted = layer(storage, identities, ordered=ordered)
        storage.admit(admitted.reference)
        rows = [row for batch in storage.lookup_batches(admitted.reference, ['e\u0301', 'é', 'absent']) for row in batch.to_pylist()]
        assert [row['record_identity'] for row in rows] == ['e\u0301', 'é']
        assert storage.lookup(admitted.reference, 'z' * 10_000)['value'] == 'z' * 10_000


def test_disjoint_union_keeps_base_files_and_checks_overlap(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as storage:
        base = layer(storage, [f'root-{i:04}' for i in range(40)])
        delta = layer(storage, ['added-1', 'added-2'])
        combined = storage.union_disjoint(base, delta)
        assert {f['path'] for f in files(storage, base)} <= {f['path'] for f in files(storage, combined)}
        storage.admit(combined.reference)
        assert [row['id'] for row in storage.stream(combined.reference)] == ['added-1', 'added-2', *[f'root-{i:04}' for i in range(40)]]
        overlapping = layer(storage, ['root-0000', 'root-9999'])
        with pytest.raises(IntegrityError, match='disjoint'):
            storage.union_disjoint(base, overlapping)
        filtered = storage.union_disjoint(base, overlapping, exclude_existing=True)
        assert filtered.reference.record_count == 41
        assert storage.lookup(filtered.reference, 'root-9999')['value'] == 'root-9999'
