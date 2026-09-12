"""Serial and spawned-worker derivation over bounded catalog partitions."""

from __future__ import annotations

import heapq
import os
import pickle
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any

from rulespec_artifacts import canonical_json_bytes

from docspec.adapters.catalog_artifact.accounting import _accumulate_join_coverage, _DispositionTally
from docspec.adapters.catalog_artifact.digests import (
    _DerivedCatalog,
    _detail_count_digests,
    _finish_derivation,
    _fixed_count_digests,
    _interpretation_records_for,
    _joined_field_records_for,
    _normalized_field_records_for,
    _rendition_choice_record,
)
from docspec.adapters.catalog_artifact.rows import _iter_catalog_rows, _iter_partition_stream
from docspec.adapters.catalog_artifact.rules import _SELECTED_DISPOSITION, _CatalogPartition, _utf16_key
from docspec.domain.source_catalog import (
    SOURCE_CATALOG_MAX_JOIN_IDS,
)
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import (
    SourceCatalogBlobSource,
)

_PARALLEL_ROW_THRESHOLD = 5_000


_MAX_DERIVE_WORKERS = 8


#: How long the worker probe waits before the derivation gives up on workers.
#: A healthy pool answers in 0.13 s with eight workers, since Pool starts them
#: lazily and the first one to come up replies, so this is ~460x headroom. It
#: is not a performance knob: it only elapses when workers cannot run at all,
#: and then the serial derivation still produces the identical result.
_PARALLEL_PROBE_TIMEOUT_SECONDS = 60.0


def _derive_worker_count(item_count: int, workers: int | None) -> int:
    if workers is not None:
        return max(1, workers)
    if item_count < _PARALLEL_ROW_THRESHOLD:
        return 1
    return max(1, min(_MAX_DERIVE_WORKERS, os.cpu_count() or 1))


def _row_digest_payloads(
    row: Mapping[str, Any],
    raw: bytes,
) -> tuple[
    bytes,
    bytes,
    bytes | None,
    bytes,
    bytes,
    bytes,
    list[bytes],
    list[tuple[bytes, str, str]],
    list[bytes],
]:
    """Build every framed payload one row contributes, in one place.

    The serial engine and the parallel workers both call this, so the bytes a
    digest consumes cannot depend on which path derived them.
    """

    source_item_id = row["sourceItemId"]
    disposition = row["selection"]["disposition"]
    selected_payload = (
        canonical_json_bytes({"sourceItemId": source_item_id, "documentId": row["documentId"]})
        if disposition == _SELECTED_DISPOSITION
        else None
    )
    joined: list[tuple[bytes, str, str]] = []
    for record in _joined_field_records_for(row):
        # Carry the identity and outcome out with the payload. The worker used
        # to recover joinId by json.loads-ing bytes it had just serialized from
        # a record that held it.
        joined.append((canonical_json_bytes(record), str(record["outcome"]), str(record["joinId"])))
    return (
        raw,
        canonical_json_bytes({"sourceItemId": source_item_id}),
        selected_payload,
        canonical_json_bytes({"sourceItemId": source_item_id, "disposition": disposition}),
        canonical_json_bytes({"sourceItemId": source_item_id, "reason": row["selection"]["reason"]}),
        canonical_json_bytes(_rendition_choice_record(row)),
        [canonical_json_bytes(r) for r in _normalized_field_records_for(row)],
        joined,
        [canonical_json_bytes(r) for r in _interpretation_records_for(row)],
    )


def _parallel_probe() -> bool:
    """A no-op worker task proving this interpreter can host spawned workers."""

    return True


def _derive_pool_context() -> Any:
    """The spawn context derive pools start from; a seam for tests.

    Named the way SpicySearch names the same seam in its snapshot build, so a
    test can observe what crosses the process boundary without reaching into
    :mod:`multiprocessing`.
    """

    import multiprocessing

    return multiprocessing.get_context("spawn")


def _derive_partition_worker(
    args: tuple[str, Any, int, bool, str],
) -> tuple[str, str, int, _DispositionTally, dict[str, dict[str, int]], int, int, int]:
    """Process one partition's rows in a subprocess and spill ordered payloads.

    Returns (partition_id, spill_path, row_count, tally,
    join_counts, normalized_count, joined_count, interpretation_count). The
    spill holds one pickled payload tuple per row, in partition order; global
    ordering across partitions is enforced by the parent's merge.

    ``args[1]`` is a duplicated descriptor for the partition blob, already
    opened through the parent's pinned blob source, so the rows arrive as a
    stream this worker reads at its own pace rather than as a bytes copy of the
    whole partition. See :func:`_derive_catalog_parallel` for why.
    """

    partition_id, blob, record_count, validate, spill_dir = args
    spill_path = os.path.join(spill_dir, f"{partition_id}.rows")
    tally = _DispositionTally()
    join_counts: dict[str, dict[str, int]] = {}
    normalized_count = 0
    joined_count = 0
    interpretation_count = 0
    rows = 0
    descriptor = blob.detach()
    # A duplicated descriptor shares its file offset with the one it came from,
    # so start from the beginning rather than from wherever the parent left it.
    os.lseek(descriptor, 0, os.SEEK_SET)
    with open(spill_path, "wb") as spill, os.fdopen(descriptor, "rb") as payload:
        for row, raw in _iter_partition_stream(
            payload,
            partition_id=partition_id,
            record_count=record_count,
            validate=validate,
            with_raw=True,
            as_dict=True,
        ):
            payloads = _row_digest_payloads(row, raw)
            tally.add(row["selection"]["disposition"], row["selection"]["reasonCode"])
            normalized_count += len(payloads[6])
            for _record_bytes, outcome, join_id in payloads[7]:
                joined_count += 1
                _accumulate_join_coverage(join_counts, {"joinId": join_id, "outcome": outcome})
            interpretation_count += len(payloads[8])
            pickle.dump(
                (_utf16_key(row["sourceItemId"]), payloads),
                spill,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            rows += 1
    return (
        partition_id,
        spill_path,
        rows,
        tally,
        join_counts,
        normalized_count,
        joined_count,
        interpretation_count,
    )


def _iter_spill(spill_path: str) -> Iterator[tuple[bytes, tuple[Any, ...]]]:
    with open(spill_path, "rb") as spill:
        while True:
            try:
                yield pickle.load(spill)
            except EOFError:
                return


def _derive_catalog(
    blob_source: SourceCatalogBlobSource,
    partitions: Sequence[_CatalogPartition],
    *,
    item_count: int,
    selected_count: int,
    validate_rows: bool = True,
    workers: int | None = None,
) -> _DerivedCatalog:
    """Derive the catalog's digests and diagnostics in two streamed passes.

    Pass one validates every row exactly once and feeds each fixed-count framed
    digest incrementally; the staged row bytes are proven canonical by the
    parse, and item round-tripping is byte-exact (pinned by test), so the state
    digest frames the raw row bytes instead of re-serializing. The three
    diagnostics whose framed sections declare data-dependent counts are counted
    in pass one and hashed in pass two, which re-reads rows without repeating
    the schema validation pass one already performed. The per-row ordering the
    old per-digest generators re-checked is enforced once, globally, by
    ``_iter_located_catalog_rows``.
    """

    worker_count = _derive_worker_count(item_count, workers)
    if worker_count > 1 and len(partitions) > 1:
        return _derive_catalog_parallel(
            blob_source,
            partitions,
            item_count=item_count,
            selected_count=selected_count,
            validate_rows=validate_rows,
            worker_count=worker_count,
        )
    fixed_digests = _fixed_count_digests(item_count, selected_count)
    state, requested, selected, dispositions, reasons, rendition_choices = fixed_digests
    tally = _DispositionTally()
    join_counts: dict[str, dict[str, int]] = {}
    normalized_count = 0
    joined_count = 0
    interpretation_count = 0
    for row, raw in _iter_catalog_rows(
        blob_source, partitions, item_count, validate=validate_rows, with_raw=True, as_dict=True
    ):
        source_item_id = row["sourceItemId"]
        disposition = row["selection"]["disposition"]
        state.add_payload(raw)
        requested.add({"sourceItemId": source_item_id})
        tally.add(disposition, row["selection"]["reasonCode"])
        if disposition == _SELECTED_DISPOSITION:
            selected.add({"sourceItemId": source_item_id, "documentId": row["documentId"]})
        dispositions.add({"sourceItemId": source_item_id, "disposition": disposition})
        reasons.add({"sourceItemId": source_item_id, "reason": row["selection"]["reason"]})
        rendition_choices.add(_rendition_choice_record(row))
        for _record in _normalized_field_records_for(row):
            normalized_count += 1
        for record in _joined_field_records_for(row):
            joined_count += 1
            _accumulate_join_coverage(join_counts, record)
        for _record in _interpretation_records_for(row):
            interpretation_count += 1
    detail_digests = _detail_count_digests(normalized_count, joined_count, interpretation_count)
    normalized, joined, interpretations = detail_digests
    for row in _iter_catalog_rows(blob_source, partitions, item_count, validate=False, as_dict=True):
        for record in _normalized_field_records_for(row):
            normalized.add(record)
        for record in _joined_field_records_for(row):
            joined.add(record)
        for record in _interpretation_records_for(row):
            interpretations.add(record)
    return _finish_derivation(fixed_digests, detail_digests, tally, join_counts, {"path": "serial", "workers": 1})


def _derive_catalog_parallel(
    blob_source: SourceCatalogBlobSource,
    partitions: Sequence[_CatalogPartition],
    *,
    item_count: int,
    selected_count: int,
    validate_rows: bool,
    worker_count: int,
) -> _DerivedCatalog:
    """Derive the same digests with per-partition workers and one ordered merge.

    Workers own every expensive per-row step -- parse, schema check, item
    construction, projection canonicalization -- and spill ordered digest-ready
    payloads built by the same helpers the serial engine uses. The parent sums
    the counters, initializes every framed hasher with known counts, and feeds
    them from one heap-merge of the ordered spills, so the byte stream each
    digest consumes is identical to the serial derivation's. When several
    partitions carry defects, which partition's refusal surfaces first may
    differ from the serial order; the refusals themselves are identical.

    Each task carries a *descriptor* for its partition blob, not the blob. The
    parent once read each partition whole and pickled those bytes to a worker,
    which windowed the number of partitions in flight but not their size:
    ``CATALOG_PARTITION_BUCKET_COUNT`` is a fixed 64, so a partition is always
    1/64th of the catalog and peak memory was a fixed *fraction* of the corpus
    rather than a bound on it -- 16 in the parent's queue plus one per worker.
    Measured, that put a 1.17 GB catalog's derivation at 1,956 MB across the
    process tree and a real 7.61 GB catalog's build at 2.78 GB, growing without
    limit. Streaming makes both ends O(one buffered read) instead.

    The descriptor is duplicated from an open the parent performed through its
    pinned blob source, so a worker inherits exactly the file the parent
    resolved and verified; handing over a path instead would reintroduce the
    re-resolution that :mod:`docspec.adapters.source_catalog_store` pins
    against. A sibling with the same problem, SpicySearch's snapshot build,
    bounds its worker arguments by shipping fixed-size row chunks instead;
    that also works, but DocSpec checks record count, strict ordering and
    bucket membership per partition in ``_iter_partition_stream``, and keeping
    the partition whole keeps those checks exactly as they were.
    """

    import tempfile
    from multiprocessing import reduction

    context = _derive_pool_context()
    ordered = sorted(partitions, key=lambda value: _utf16_key(value.partition_id))
    with tempfile.TemporaryDirectory(prefix="docspec-catalog-derive-") as spill_dir:

        def task_arguments() -> Iterator[tuple[str, Any, int, bool, str]]:
            for partition in ordered:
                member = partition.member
                if member.blob_ref is None or member.record_count is None:
                    raise IntegrityError("source-item partition descriptor requires blobRef and recordCount")
                with blob_source.open(member.blob_ref) as stream:
                    # DupFd duplicates the descriptor as it is constructed, so
                    # the pinned open can close here and the worker still holds
                    # the same file.
                    blob = reduction.DupFd(stream.fileno())
                yield (
                    partition.partition_id,
                    blob,
                    member.record_count,
                    validate_rows,
                    spill_dir,
                )

        summaries: list[tuple[str, str, int, dict[str, int], dict[str, dict[str, int]], int, int, int]] = []
        arguments = task_arguments()
        with context.Pool(worker_count) as pool:
            try:
                # Must be the timed asynchronous form. An interpreter whose
                # __main__ cannot be re-imported (frozen, embedded, stdin)
                # cannot host spawned workers -- but the child fails during its
                # own bootstrap, and Pool answers a dead worker by starting
                # another one, forever. The blocking pool.apply() therefore
                # never returns and never raises for exactly the case this
                # fallback exists to handle: measured, a derivation driven from
                # a stdin script span workers until it was killed. Only a
                # timeout can observe it.
                pool.apply_async(_parallel_probe).get(timeout=_PARALLEL_PROBE_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - workers are unavailable here;
                # the serial derivation is always available and identical.
                fallback = _derive_catalog(
                    blob_source,
                    partitions,
                    item_count=item_count,
                    selected_count=selected_count,
                    validate_rows=validate_rows,
                    workers=1,
                )
                return replace(fallback, derivation={"path": "serial-fallback", "workers": 1})
            pending: list[Any] = []

            def submit_next() -> bool:
                try:
                    args = next(arguments)
                except StopIteration:
                    return False
                pending.append(pool.apply_async(_derive_partition_worker, (args,)))
                return True

            for _ in range(worker_count * 2):
                if not submit_next():
                    break
            while pending:
                summaries.append(pending.pop(0).get())
                submit_next()

        tally = _DispositionTally()
        join_counts: dict[str, dict[str, int]] = {}
        normalized_count = 0
        joined_count = 0
        interpretation_count = 0
        total_rows = 0
        for summary in sorted(summaries, key=lambda value: _utf16_key(value[0])):
            _, _, rows, partition_tally, partition_joins, n_count, j_count, i_count = summary
            total_rows += rows
            tally.merge(partition_tally)
            for join_id in sorted(partition_joins, key=_utf16_key):
                if join_id not in join_counts and len(join_counts) >= SOURCE_CATALOG_MAX_JOIN_IDS:
                    raise LimitExceededError("catalog join coverage exceeds its distinct-identity limit")
                target = join_counts.setdefault(
                    join_id,
                    {"eligible": 0, "matched": 0, "unmatched": 0, "nullResult": 0},
                )
                for name in target:
                    target[name] += partition_joins[join_id][name]
            normalized_count += n_count
            joined_count += j_count
            interpretation_count += i_count
        if total_rows != item_count:
            raise IntegrityError("source-catalog row count differs from its partition descriptors")

        fixed_digests = _fixed_count_digests(item_count, selected_count)
        state, requested, selected, dispositions, reasons, rendition_choices = fixed_digests
        detail_digests = _detail_count_digests(normalized_count, joined_count, interpretation_count)
        normalized, joined, interpretations = detail_digests
        previous: bytes | None = None
        spill_streams = [_iter_spill(summary[1]) for summary in summaries]
        for key, payloads in heapq.merge(*spill_streams, key=lambda entry: entry[0]):
            if previous is not None and key <= previous:
                raise IntegrityError("source-catalog rows must be globally ordered and distinct")
            previous = key
            state.add_payload(payloads[0])
            requested.add_payload(payloads[1])
            if payloads[2] is not None:
                selected.add_payload(payloads[2])
            dispositions.add_payload(payloads[3])
            reasons.add_payload(payloads[4])
            rendition_choices.add_payload(payloads[5])
            for record_bytes in payloads[6]:
                normalized.add_payload(record_bytes)
            for record_bytes, _outcome, _join_id in payloads[7]:
                joined.add_payload(record_bytes)
            for record_bytes in payloads[8]:
                interpretations.add_payload(record_bytes)
        return _finish_derivation(
            fixed_digests, detail_digests, tally, join_counts, {"path": "parallel", "workers": worker_count}
        )
