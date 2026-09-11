"""Deterministic catalog derivation across serial and process-worker execution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.catalog_artifact import derivation as catalog_derivation
from docspec.adapters.catalog_artifact import rules as catalog_rules
from docspec.adapters.catalog_artifact import verification as catalog_verification
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.domain.source_catalog import CatalogDisposition
from tests.support.source_catalog import FakeSource, description, producer, record, renditions
from tests.support.source_catalog_builds import (
    build,
)


def test_the_parallel_derivation_is_byte_identical_to_the_serial_one(tmp_path: Path) -> None:
    """Workers may change wall time, never a digest.

    Builds a real multi-partition catalog, then derives serially and with two
    forced workers: every digest, count, and diagnostic must be identical --
    the parallel path spills the same payload bytes the serial helpers build
    and merges them in the same global order.
    """

    from rulespec_artifacts import LocalMemberSource, admit_artifact

    identities = [f"2026-0000{i}" for i in range(1, 8)]
    source = FakeSource(
        description(),
        tuple(record(identity) for identity in identities),
        tuple(r for identity in identities for r in renditions(identity)),
    )
    store, result = build(tmp_path, source)
    reader = SourceCatalogArtifactReader(store, producer=producer())
    summary = reader.verify_snapshot(result.reference)

    blob_source = store.blob_source()
    artifact_root = Path(store.root) / result.reference.digest.removeprefix("sha256:")
    verifier = catalog_verification.SourceCatalogArtifactVerifier(producer(), blob_source)
    admit_artifact(
        LocalMemberSource(artifact_root),
        blob_source=blob_source,
        expected_pin=None,
        scratch_directory=tmp_path / "admit-scratch",
        semantic_verifier=verifier,
    )
    assert len(verifier.partitions) > 1, "test needs a multi-partition catalog"

    selected_count = summary.disposition_counts[
        CatalogDisposition.SELECTED.value
    ]
    serial = catalog_derivation._derive_catalog(
        blob_source,
        verifier.partitions,
        item_count=summary.item_count,
        selected_count=selected_count,
        workers=1,
    )
    parallel = catalog_derivation._derive_catalog(
        blob_source,
        verifier.partitions,
        item_count=summary.item_count,
        selected_count=selected_count,
        workers=2,
    )
    # Everything but the engine's own name must agree; the name must not.
    assert replace(parallel, derivation={}) == replace(serial, derivation={})
    assert serial.derivation == {"path": "serial", "workers": 1}
    assert parallel.derivation == {"path": "parallel", "workers": 2}
    assert serial.catalog_state_digest == summary.catalog_state_digest


def test_the_automatic_worker_count_resolves_on_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto path must not depend on APIs newer than the pinned Python.

    Explicit worker counts pass straight through; below the threshold the
    derivation stays serial; at or above it the count resolves from the real
    interpreter's CPU API (this call is the regression: it once used a 3.13-only
    function that every explicit-workers test skipped past).
    """

    resolve = catalog_derivation._derive_worker_count
    assert resolve(10, 3) == 3
    assert resolve(10, None) == 1
    monkeypatch.setattr(catalog_derivation, "_PARALLEL_ROW_THRESHOLD", 5)
    automatic = resolve(10, None)
    assert 1 <= automatic <= catalog_derivation._MAX_DERIVE_WORKERS


def test_derive_workers_receive_a_stream_not_the_partition_bytes(tmp_path: Path) -> None:
    """What crosses the process boundary must not grow with the partition.

    The parallel derivation once read each partition whole and pickled the
    bytes to a worker. Because the bucket count is fixed, a partition is always
    1/64th of the catalog, so that made peak memory a fixed fraction of the
    corpus -- measured at 2.78 GB for a real 7.61 GB catalog -- rather than a
    bound on it.

    The equivalence test above cannot see this: it passes byte-identically
    whether the payload is streamed or copied. So assert the property directly.
    Every task argument, with the descriptor handle scrubbed out, must be
    smaller than the smallest partition it stands for; that can only hold while
    the payload is absent from the arguments. The partition sizes and count are
    asserted too, so the test cannot pass by deriving nothing.
    """

    import pickle

    from rulespec_artifacts import LocalMemberSource, admit_artifact

    identities = [f"2026-0000{i}" for i in range(1, 8)]
    source = FakeSource(
        description(),
        tuple(record(identity) for identity in identities),
        tuple(r for identity in identities for r in renditions(identity)),
    )
    store, result = build(tmp_path, source)
    reader = SourceCatalogArtifactReader(store, producer=producer())
    summary = reader.verify_snapshot(result.reference)
    blob_source = store.blob_source()
    artifact_root = Path(store.root) / result.reference.digest.removeprefix("sha256:")
    verifier = catalog_verification.SourceCatalogArtifactVerifier(producer(), blob_source)
    admit_artifact(
        LocalMemberSource(artifact_root),
        blob_source=blob_source,
        expected_pin=None,
        scratch_directory=tmp_path / "admit-scratch",
        semantic_verifier=verifier,
    )
    partitions = verifier.partitions
    assert len(partitions) > 1, "test needs a multi-partition catalog"
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
            self._pool = pool

        def __enter__(self) -> RecordingPool:
            self._pool.__enter__()
            return self

        def __exit__(self, *details: object) -> Any:
            return self._pool.__exit__(*details)

        def apply(self, function: Any, *args: Any, **kwargs: Any) -> Any:
            return self._pool.apply(function, *args, **kwargs)

        def apply_async(self, function: Any, args: tuple[Any, ...] = (), **kwargs: Any) -> Any:
            if args:  # the worker probe carries none; only real tasks are measured
                recorded.append(scrubbed_size(args[0]))
            return self._pool.apply_async(function, args, **kwargs)

    inner = catalog_derivation._derive_pool_context()

    class RecordingContext:
        def Pool(self, *args: Any, **kwargs: Any) -> RecordingPool:
            return RecordingPool(inner.Pool(*args, **kwargs))

    selected_count = summary.disposition_counts[
        CatalogDisposition.SELECTED.value
    ]
    serial = catalog_derivation._derive_catalog(
        blob_source,
        partitions,
        item_count=summary.item_count,
        selected_count=selected_count,
        workers=1,
    )
    original = catalog_derivation._derive_pool_context
    catalog_derivation._derive_pool_context = RecordingContext  # type: ignore[assignment]
    try:
        parallel = catalog_derivation._derive_catalog(
            blob_source,
            partitions,
            item_count=summary.item_count,
            selected_count=selected_count,
            workers=2,
        )
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
    """The probe must time out, because a dead worker never raises.

    _derive_catalog_parallel guards itself with a no-op probe so that an
    interpreter whose __main__ cannot be re-imported -- frozen, embedded, or a
    script fed on stdin -- falls back to the serial derivation. The blocking
    pool.apply() could not do that: the child fails inside its own bootstrap,
    and Pool responds to a dead worker by starting another, forever. Measured,
    a derivation driven from a stdin script spawned workers until it was
    killed, so the guard never fired for the exact case it names.

    Assert the shape that makes the guard work: the probe goes through the
    timed asynchronous form, and a probe that does not answer falls back to a
    serial derivation whose result is identical.
    """

    import multiprocessing

    from rulespec_artifacts import LocalMemberSource, admit_artifact

    identities = [f"2026-0000{i}" for i in range(1, 8)]
    source = FakeSource(
        description(),
        tuple(record(identity) for identity in identities),
        tuple(r for identity in identities for r in renditions(identity)),
    )
    store, result = build(tmp_path, source)
    reader = SourceCatalogArtifactReader(store, producer=producer())
    summary = reader.verify_snapshot(result.reference)
    blob_source = store.blob_source()
    artifact_root = Path(store.root) / result.reference.digest.removeprefix("sha256:")
    verifier = catalog_verification.SourceCatalogArtifactVerifier(producer(), blob_source)
    admit_artifact(
        LocalMemberSource(artifact_root),
        blob_source=blob_source,
        expected_pin=None,
        scratch_directory=tmp_path / "admit-scratch",
        semantic_verifier=verifier,
    )
    assert len(verifier.partitions) > 1, "test needs a multi-partition catalog"

    class NeverAnswers:
        def get(self, timeout: float | None = None) -> Any:
            assert timeout is not None, (
                "the probe must pass a timeout: a worker that dies during "
                "bootstrap makes Pool respawn it forever, so an untimed wait "
                "never returns and never raises"
            )
            raise multiprocessing.TimeoutError

    class DeadPool:
        def __enter__(self) -> DeadPool:
            return self

        def __exit__(self, *details: object) -> bool:
            return False

        def apply(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "the probe must not use the blocking apply(); see NeverAnswers"
            )

        def apply_async(self, *args: Any, **kwargs: Any) -> NeverAnswers:
            return NeverAnswers()

    class DeadContext:
        def Pool(self, *args: Any, **kwargs: Any) -> DeadPool:
            return DeadPool()

    selected_count = summary.disposition_counts[
        CatalogDisposition.SELECTED.value
    ]
    serial = catalog_derivation._derive_catalog(
        blob_source,
        verifier.partitions,
        item_count=summary.item_count,
        selected_count=selected_count,
        workers=1,
    )
    original = catalog_derivation._derive_pool_context
    catalog_derivation._derive_pool_context = DeadContext  # type: ignore[assignment]
    try:
        fell_back = catalog_derivation._derive_catalog(
            blob_source,
            verifier.partitions,
            item_count=summary.item_count,
            selected_count=selected_count,
            workers=2,
        )
    finally:
        catalog_derivation._derive_pool_context = original  # type: ignore[assignment]

    assert replace(fell_back, derivation={}) == replace(serial, derivation={})
    assert fell_back.derivation == {"path": "serial-fallback", "workers": 1}
    assert fell_back.catalog_state_digest == summary.catalog_state_digest


def test_derivation_names_the_engine_that_produced_the_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parallel engine falls back to the serial one silently when its
    workers cannot start, and nothing recorded which path a receipt's digests
    came from -- so a week of timing numbers were unscoped. Now the result says,
    for the build and for the gate, and the reference is identical on all three
    paths, which is what makes the fallback safe and the timing scopable.
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
