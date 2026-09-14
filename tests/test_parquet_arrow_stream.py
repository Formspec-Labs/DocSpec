from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier, get_ident
from typing import Any

import pytest

from docspec.adapters.storage.records import IcebergRecordStorage
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import LimitExceededError


SCHEMA = RecordSchema(
    "docspec-test-record/1.0",
    ("recordId", "sourceItemId", "value"),
    "recordId",
    "sourceItemId",
)
POLICY = PartitionPolicy("source-item-sha256-v1", 8)


def _row(index: int) -> dict[str, Any]:
    return {
        "recordId": f"record-{index:05d}",
        "sourceItemId": f"source-{index % 17:02d}",
        "value": {"index": index, "text": "café", "optional": None},
    }


@pytest.mark.parametrize("error_type", [ValueError, LimitExceededError])
@pytest.mark.parametrize("fail_after", [0, 4097])
def test_arrow_producer_failure_preserves_error_and_closes_input(
    tmp_path: Path, error_type: type[Exception], fail_after: int,
) -> None:
    failure = error_type("input producer failed")

    class Source:
        def __init__(self) -> None:
            self.index = 0
            self.close_count = 0

        def __iter__(self) -> Source:
            return self

        def __next__(self) -> dict[str, Any]:
            if self.index == fail_after:
                raise failure
            row = _row(self.index)
            self.index += 1
            return row

        def close(self) -> None:
            self.close_count += 1

    source = Source()
    with closing(IcebergRecordStorage(tmp_path / "records")) as storage:
        with pytest.raises(error_type) as caught:
            storage.write_layer(
                source, layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
            )
        assert caught.value is failure
        assert source.index == fail_after
        assert source.close_count == 1
        assert not list(storage.root.rglob("*.parquet"))
        assert not list(storage.root.rglob("*.json"))
        assert not list((storage.root / ".staging").iterdir())

        healthy = storage.write_layer(
            [_row(0)], layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
        )
        storage.verify(healthy)
        assert list(storage.stream(healthy)) == [_row(0)]


def test_arrow_input_can_stream_from_the_same_storage_connection(tmp_path: Path) -> None:
    count = 5001
    closed = 0
    with closing(IcebergRecordStorage(tmp_path / "records")) as storage:
        original = storage.write_layer(
            (_row(index) for index in range(count)),
            layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
        )

        def source() -> Iterator[dict[str, Any]]:
            nonlocal closed
            try:
                with closing(storage.stream(original)) as rows:
                    yield from rows
            finally:
                closed += 1

        copied = storage.write_layer(
            source(), layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
        )
        assert closed == 1
        storage.verify(copied)
        with closing(storage.stream(copied)) as actual:
            for row, index in zip(actual, range(count), strict=True):
                assert row == _row(index)
        assert not list((storage.root / ".staging").iterdir())


@pytest.mark.parametrize("callers", [1, 2], ids=["single-caller", "concurrent-callers"])
def test_arrow_sqlite_input_stays_on_each_callers_thread(tmp_path: Path, callers: int) -> None:
    count = 2049
    ready = Barrier(callers)
    with closing(IcebergRecordStorage(tmp_path / "records")) as storage:
        def write(caller: int):
            owner = get_ident()
            consumed_on: list[int] = []
            closed_on: list[int] = []
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("CREATE TABLE source (n INTEGER PRIMARY KEY)")
                connection.executemany("INSERT INTO source VALUES (?)", ((n,) for n in range(count)))

                def rows() -> Iterator[dict[str, Any]]:
                    consumed_on.append(get_ident())
                    try:
                        with closing(connection.execute("SELECT n FROM source ORDER BY n")) as cursor:
                            for (n,) in cursor:
                                assert get_ident() == owner
                                yield _row(caller * count + n)
                    finally:
                        closed_on.append(get_ident())

                ready.wait(timeout=10)
                reference = storage.write_layer(
                    rows(), layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
                )
            assert consumed_on == [owner]
            assert closed_on == [owner]
            return reference

        if callers == 1:
            references = [write(0)]
        else:
            with ThreadPoolExecutor(max_workers=callers) as pool:
                references = list(pool.map(write, range(callers)))
        for caller, reference in enumerate(references):
            storage.verify(reference)
            with closing(storage.stream(reference)) as actual:
                for row, index in zip(actual, range(count), strict=True):
                    assert row == _row(caller * count + index)
        assert not list((storage.root / ".staging").iterdir())
