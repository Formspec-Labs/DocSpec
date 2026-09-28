"""Catalog derivation worker contract: the parallel path is byte-identical to the serial one, workers receive a
streamed partition handle rather than partition bytes, and a pool that never starts falls back to a serial
derivation whose result and reference are identical.

Also pins the automatic worker count resolving on the pinned interpreter, task arguments staying smaller than
the partitions they stand for, the derivation recording which engine (serial, parallel or serial-fallback)
produced the build and gate digests, and that a worker which dies mid-task or ignores shutdown fails the
derivation in bounded time instead of hanging it.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.catalog_artifact import derivation as catalog_derivation
from docspec.adapters.catalog_artifact import rules as catalog_rules
from docspec.adapters.catalog_artifact import verification as catalog_verification
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.domain.source_catalog import CatalogDisposition
from tests.support import derive_workers
from tests.support.source_catalog import FakeSource, description, producer, record, renditions
from tests.support.source_catalog_builds import (
    build,
)


def _partitioned_catalog(tmp_path: Path) -> tuple[Any, tuple[Any, ...], Any]:
    """A built and admitted seven-item catalog: its blob source, its verified partitions (several) and summary."""

    from rulespec_artifacts import LocalMemberSource, admit_artifact

    identities = [f"2026-0000{i}" for i in range(1, 8)]
    source = FakeSource(
        description(),
        tuple(record(identity) for identity in identities),
        tuple(r for identity in identities for r in renditions(identity)),
    )
    store, result = build(tmp_path, source)
    summary = SourceCatalogArtifactReader(store, producer=producer()).verify_snapshot(result.reference)
    blob_source = store.blob_source()
    verifier = catalog_verification.SourceCatalogArtifactVerifier(producer(), blob_source)
    admit_artifact(
        LocalMemberSource(Path(store.root) / result.reference.digest.removeprefix("sha256:")),
        blob_source=blob_source,
        expected_pin=None,
        scratch_directory=tmp_path / "admit-scratch",
        semantic_verifier=verifier,
    )
    assert len(verifier.partitions) > 1, "test needs a multi-partition catalog"
    return blob_source, verifier.partitions, summary


def _derive(blob_source: Any, partitions: Any, summary: Any, workers: int) -> Any:
    return catalog_derivation._derive_catalog(
        blob_source,
        partitions,
        item_count=summary.item_count,
        selected_count=summary.disposition_counts[CatalogDisposition.SELECTED.value],
        workers=workers,
    )


def test_the_parallel_derivation_is_byte_identical_to_the_serial_one(tmp_path: Path) -> None:
    """Workers may change wall time, never a digest: serial and two-worker derivation of a real multi-partition
    catalog agree on every digest, count and diagnostic, because both spill the same payload bytes and merge
    them in the same global order.
    """

    blob_source, partitions, summary = _partitioned_catalog(tmp_path)
    serial = _derive(blob_source, partitions, summary, 1)
    parallel = _derive(blob_source, partitions, summary, 2)
    # Everything but the engine's own name must agree; the name must not.
    assert replace(parallel, derivation={}) == replace(serial, derivation={})
    assert serial.derivation == {"path": "serial", "workers": 1}
    assert parallel.derivation == {"path": "parallel", "workers": 2}
    assert serial.catalog_state_digest == summary.catalog_state_digest


def test_the_automatic_worker_count_resolves_on_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto path must not depend on APIs newer than the pinned Python: explicit counts pass straight
    through, below the threshold it stays serial, and at or above it resolves from the real interpreter's CPU
    API (this call once used a 3.13-only function that every explicit-workers test skipped past).
    """

    resolve = catalog_derivation._derive_worker_count
    assert resolve(10, 3) == 3
    assert resolve(10, None) == 1
    monkeypatch.setattr(catalog_derivation, "_PARALLEL_ROW_THRESHOLD", 5)
    automatic = resolve(10, None)
    assert 1 <= automatic <= catalog_derivation._MAX_DERIVE_WORKERS


def test_derive_workers_receive_a_stream_not_the_partition_bytes(tmp_path: Path) -> None:
    """What crosses the process boundary must not grow with the partition: the parallel path once pickled whole
    partition bytes to workers, making peak memory a fixed fraction of the corpus (measured at 2.78 GB for a
    7.61 GB catalog); every task argument, with the descriptor handle scrubbed, must now be smaller than the
    smallest partition it stands for, and partition sizes and count are asserted so the test cannot pass by
    deriving nothing.
    """

    import pickle

    blob_source, partitions, summary = _partitioned_catalog(tmp_path)
    partition_sizes = [value.member.byte_size or 0 for value in partitions]
    assert min(partition_sizes) > 512, "test needs partitions with real bytes in them"

    recorded: list[int] = []

    def scrubbed_size(arguments: tuple[Any, ...]) -> int:
        # Measure the data an argument carries, standing in for anything that
        # is not plain data. Pickling the live descriptor handle would register
        # a second descriptor with the resource sharer that nothing collects,
        # and its size says nothing about the payload either way. A payload
        # smuggled back in as bytes or text is still measured.
        return len(
            pickle.dumps(
                tuple(
                    value
                    if isinstance(value, (str, int, bool, bytes, bytearray))
                    else "<handle>"
                    for value in arguments
                )
            )
        )

    class RecordingPool:
        def __init__(self, pool: Any) -> None:
            self._inner = pool

        @property
        def _pool(self) -> Any:
            return self._inner._pool

        def terminate(self) -> None:
            self._inner.terminate()

        def apply_async(self, function: Any, args: tuple[Any, ...] = (), **kwargs: Any) -> Any:
            if args:  # the worker probe carries none; only real tasks are measured
                recorded.append(scrubbed_size(args[0]))
            return self._inner.apply_async(function, args, **kwargs)

    inner = catalog_derivation._derive_pool_context()

    class RecordingContext:
        def Pool(self, *args: Any, **kwargs: Any) -> RecordingPool:
            return RecordingPool(inner.Pool(*args, **kwargs))

    serial = _derive(blob_source, partitions, summary, 1)
    original = catalog_derivation._derive_pool_context
    catalog_derivation._derive_pool_context = RecordingContext  # type: ignore[assignment]
    try:
        parallel = _derive(blob_source, partitions, summary, 2)
    finally:
        catalog_derivation._derive_pool_context = original  # type: ignore[assignment]

    assert replace(parallel, derivation={}) == replace(serial, derivation={}), (
        "streaming must not change a single derived value"
    )
    assert parallel.derivation["path"] == "parallel"
    assert len(recorded) == len(partitions), "every partition must be submitted once"
    assert max(recorded) < min(partition_sizes), (
        f"task arguments carry the partition payload: largest argument "
        f"{max(recorded)} B against smallest partition {min(partition_sizes)} B"
    )


def test_a_worker_pool_that_never_starts_falls_back_instead_of_hanging(tmp_path: Path) -> None:
    """The probe must time out, because a dead worker never raises: the guard uses the timed asynchronous form
    so an interpreter whose __main__ cannot be re-imported (frozen, embedded or a stdin script) falls back to a
    serial derivation with an identical result, where the blocking apply() would respawn a dead worker forever
    (measured against a stdin script spawning workers until killed).
    """

    import multiprocessing

    blob_source, partitions, summary = _partitioned_catalog(tmp_path)

    class NeverAnswers:
        def get(self, timeout: float | None = None) -> Any:
            assert timeout is not None, (
                "the probe must pass a timeout: a worker that dies during "
                "bootstrap makes Pool respawn it forever, so an untimed wait "
                "never returns and never raises"
            )
            raise multiprocessing.TimeoutError

    class DeadPool:
        def terminate(self) -> None:
            pass

        def apply(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "the probe must not use the blocking apply(); see NeverAnswers"
            )

        def apply_async(self, *args: Any, **kwargs: Any) -> NeverAnswers:
            return NeverAnswers()

    class DeadContext:
        def Pool(self, *args: Any, **kwargs: Any) -> DeadPool:
            return DeadPool()

    serial = _derive(blob_source, partitions, summary, 1)
    original = catalog_derivation._derive_pool_context
    catalog_derivation._derive_pool_context = DeadContext  # type: ignore[assignment]
    try:
        fell_back = _derive(blob_source, partitions, summary, 2)
    finally:
        catalog_derivation._derive_pool_context = original  # type: ignore[assignment]

    assert replace(fell_back, derivation={}) == replace(serial, derivation={})
    assert fell_back.derivation == {"path": "serial-fallback", "workers": 1}
    assert fell_back.catalog_state_digest == summary.catalog_state_digest


def test_a_worker_killed_mid_task_fails_the_derivation_naming_its_partitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pool replaces a worker that dies mid-task and never fails its task, so the untimed wait on its result
    blocked forever. The wait notices the exit within a second and refuses, naming the partitions pending.
    """

    blob_source, partitions, summary = _partitioned_catalog(tmp_path)
    monkeypatch.setattr(catalog_derivation, "_derive_partition_worker", derive_workers.die_mid_task)
    started = time.monotonic()
    with pytest.raises(ChildProcessError, match=r"exited with code -9 while partitions .+ were pending") as refused:
        _derive(blob_source, partitions, summary, 2)
    assert time.monotonic() - started < catalog_derivation._POOL_SHUTDOWN_SECONDS + 30
    first = sorted(partitions, key=lambda value: catalog_rules._utf16_key(value.partition_id))[0].partition_id
    assert first in str(refused.value)


def test_a_worker_that_ignores_shutdown_is_killed_after_its_partition_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live worker that never returns fails its partition at the partition bound, naming it; one that also
    survives the pool's termination signal, which held two test runs at 0% CPU in Pool's join, is killed.
    """

    blob_source, partitions, summary = _partitioned_catalog(tmp_path)
    monkeypatch.setattr(catalog_derivation, "_derive_partition_worker", derive_workers.ignore_shutdown)
    monkeypatch.setattr(catalog_derivation, "_PARTITION_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(catalog_derivation, "_POOL_SHUTDOWN_SECONDS", 1.0)
    created: list[Any] = []
    inner = catalog_derivation._derive_pool_context()

    class RememberingContext:
        def Pool(self, *args: Any, **kwargs: Any) -> Any:
            created.append(inner.Pool(*args, **kwargs))
            return created[-1]

    monkeypatch.setattr(catalog_derivation, "_derive_pool_context", RememberingContext)
    first = sorted(partitions, key=lambda value: catalog_rules._utf16_key(value.partition_id))[0].partition_id
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=f"catalog partition {first} did not finish within 2 s"):
        _derive(blob_source, partitions, summary, 2)
    assert time.monotonic() - started < 30
    workers = list(created[0]._pool)
    assert workers and all(worker.exitcode is not None for worker in workers)
    assert any(worker.exitcode == -9 for worker in workers), "a worker ignoring SIGTERM must have been killed"


def test_derivation_names_the_engine_that_produced_the_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent fallback used to leave no record of which engine produced a receipt's digests, so timing
    numbers were unscoped; the result now names the path for the build and the gate, and the reference is
    identical on all three paths (serial, parallel, serial-fallback), which makes the fallback safe.
    """

    identities_by_partition: dict[str, str] = {}
    for index in range(1, 200):
        identity = f"2026-{index:05d}"
        identities_by_partition.setdefault(catalog_rules._partition_id(identity), identity)
        if len(identities_by_partition) == 3:
            break
    identities = tuple(sorted(identities_by_partition.values()))

    def source() -> FakeSource:
        return FakeSource(
            description(),
            tuple(record(identity) for identity in identities),
            tuple(value for identity in identities for value in renditions(identity)),
        )

    _, serial = build(tmp_path / "serial", source())
    assert serial.derivation == {
        "build": {"path": "serial", "workers": 1},
        "gate": {"path": "serial", "workers": 1},
    }

    monkeypatch.setattr(catalog_derivation, "_PARALLEL_ROW_THRESHOLD", 1)
    _, parallel = build(tmp_path / "parallel", source())
    assert parallel.reference == serial.reference
    assert parallel.derivation["build"]["path"] == "parallel"
    assert parallel.derivation["gate"]["path"] == "parallel"
    assert parallel.derivation["build"]["workers"] >= 2

    monkeypatch.setattr(catalog_derivation, "_PARALLEL_PROBE_TIMEOUT_SECONDS", 0.0)
    _, fallback = build(tmp_path / "fallback", source())
    assert fallback.reference == serial.reference
    assert fallback.derivation == {
        "build": {"path": "serial-fallback", "workers": 1},
        "gate": {"path": "serial-fallback", "workers": 1},
    }
