"""Validate source rows, account for the universe, and resume durable inputs."""

from __future__ import annotations

import collections
import heapq
from collections.abc import Iterator, Mapping, Sequence
from itertools import zip_longest
from typing import Any

from rulespec_artifacts import (
    canonical_json_bytes,
    sha256_digest,
)

from docspec.adapters.catalog_artifact.rules import (
    _OUTPUT_ACCOUNTING_NAMESPACE,
    _SOURCE_RECORD_FIELDS,
    _SOURCE_RENDITION_REQUIRED_FIELDS,
    _SOURCE_ROW_NAMESPACE_PREFIX,
    _UNIVERSE_ACCOUNTING_NAMESPACE,
    MAX_SOURCE_RENDITION_BYTES_PER_RECORD,
    MAX_SOURCE_RENDITIONS_PER_RECORD,
    _mapping,
    _text,
    _utf16_key,
)
from docspec.domain.identity import require_sha256
from docspec.domain.source_catalog import (
    SourceCatalogItem,
)
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import (
    FRESH_BUILD,
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    CatalogResumePoint,
    SourceCatalogPolicy,
    SourceInputSelector,
    SourceNativeDescription,
    SourceNativeRecordSource,
    SourceNativeRow,
)

#: Ordered reads one policy may make of the rows the loader staged. Source
#: artifacts are still read exactly once -- ``_load`` guarantees that
#: independently -- and this bounds only re-reads of builder-owned SQLite,
#: which cannot go stale under us. Two: one pass to build lookup indexes, one
#: to emit items.
MAX_ORDERED_PASSES = 2


def _source_rows(source: SourceNativeRecordSource) -> Iterator[tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]]:
    renditions = iter(source.iter_renditions())
    next_rendition = next(renditions, None)
    previous_record_id: str | None = None
    # Ordering keys, not the text: renditions are ordered across the whole
    # stream, so the previous key's encoding outlives the record it belonged to.
    previous_rendition_order: tuple[bytes, bytes] | None = None

    def checked_rendition(value: object) -> tuple[Mapping[str, Any], tuple[str, str]]:
        item = _mapping(value, "source-native rendition")
        fields = set(item)
        if fields != _SOURCE_RENDITION_REQUIRED_FIELDS:
            raise IntegrityError("source-native rendition has an invalid closed shape")
        key = (
            _text(item["sourceRecordId"], "source-native rendition sourceRecordId"),
            _text(item["renditionId"], "source-native rendition renditionId"),
        )
        _text(item["sourceField"], "source-native rendition sourceField")
        _text(item["mediaType"], "source-native rendition mediaType")
        locator = item["locator"]
        if locator is not None:
            _text(locator, "source-native rendition locator")
        expected_digest = item["expectedSha256"]
        if expected_digest is not None:
            try:
                require_sha256(expected_digest, "source-native rendition expectedSha256")
            except ValueError as error:
                raise IntegrityError(str(error)) from error
        expected_size = item["expectedByteSize"]
        if expected_size is not None and (
            isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0
        ):
            raise IntegrityError("source-native rendition expectedByteSize must be null or non-negative")
        return item, key

    for raw_record in source.iter_records():
        record = _mapping(raw_record, "source-native record")
        if set(record) != _SOURCE_RECORD_FIELDS:
            raise IntegrityError("source-native record has an invalid closed shape")
        record_id = _text(record["sourceRecordId"], "source-native sourceRecordId")
        _text(record["scopeId"], "source-native scopeId")
        _text(record["schemaName"], "source-native schemaName")
        _text(record["schemaVersion"], "source-native schemaVersion")
        try:
            require_sha256(record["schemaDigest"], "source-native schemaDigest")
        except ValueError as error:
            raise IntegrityError(str(error)) from error
        _mapping(record["record"], "source-native record payload")
        if not isinstance(record["fieldDiagnostics"], list):
            raise IntegrityError("source-native fieldDiagnostics must be an array")
        record_order = _utf16_key(record_id)
        if previous_record_id is not None and record_order <= _utf16_key(previous_record_id):
            raise IntegrityError("source-native records must be strictly ordered by sourceRecordId")
        previous_record_id = record_id
        selected: list[Mapping[str, Any]] = []
        selected_bytes = 0
        while next_rendition is not None:
            rendition, key = checked_rendition(next_rendition)
            key_order = (_utf16_key(key[0]), _utf16_key(key[1]))
            if previous_rendition_order is not None and key_order <= previous_rendition_order:
                raise IntegrityError("source-native renditions must be strictly ordered")
            if key_order[0] < record_order:
                raise IntegrityError("source-native rendition has no matching record")
            if key_order[0] > record_order:
                break
            previous_rendition_order = key_order
            if len(selected) >= MAX_SOURCE_RENDITIONS_PER_RECORD:
                raise LimitExceededError("source-native rendition count exceeds its per-record limit")
            rendition_bytes = len(canonical_json_bytes(rendition))
            if selected_bytes + rendition_bytes > MAX_SOURCE_RENDITION_BYTES_PER_RECORD:
                raise LimitExceededError("source-native rendition bytes exceed their per-record limit")
            selected_bytes += rendition_bytes
            selected.append(rendition)
            next_rendition = next(renditions, None)
        yield record, tuple(selected)
    if next_rendition is not None:
        raise IntegrityError("source-native rendition has no matching record")


_RESUME_NAMESPACE = "source-catalog/resume"


class _ResumeLedger:
    """Every resume point of one build, kept in the workspace it describes.

    A durable workspace outlives a killed process, and SQLite discards whatever
    followed the last commit when it is next opened, so this ledger is the whole
    of what a resumed build may trust: the identity the workspace was staged
    under (``open`` refuses any other), which inputs finished loading, whether
    the policy's pre-pass finished, and the last committed batch of staged
    items with the counts the receipt will need. Every mark commits, so the
    ledger never claims more than the file holds.

    A workspace with no ``commit`` cannot survive a kill; its ledger records
    nothing and reports a fresh build, so the temporary-workspace path is the
    build it always was.
    """

    def __init__(self, workspace: CatalogPolicyWorkspace) -> None:
        self._workspace = workspace
        self._commit = getattr(workspace, "commit", None)
        self.point = FRESH_BUILD
        self.cursor_state: Mapping[str, Any] | None = None
        self.staged_state: Mapping[str, Any] | None = None

    def _get(self, key: tuple[str, ...]) -> Mapping[str, Any] | None:
        return self._workspace.get(_RESUME_NAMESPACE, key)

    def _mark(self, key: tuple[str, ...], value: Mapping[str, Any]) -> None:
        if self._commit is None:
            return
        if self._get(key) is None:
            self._workspace.put(_RESUME_NAMESPACE, key, value)
        else:
            self._workspace.replace(_RESUME_NAMESPACE, key, value)
        self._commit()

    def open(self, identity: Mapping[str, Any]) -> CatalogResumePoint:
        """Bind the workspace to this build, or resume the one it already holds.

        ``identity`` is everything that decides the rows: catalog id, policy
        digest, schema digest, producer, and the inputs in the order given.
        Order matters because staged rows carry their source index.
        """

        if self._commit is None:
            return self.point
        stored = self._get(("build",))
        if stored is None:
            self._mark(("build",), identity)
            return self.point
        if canonical_json_bytes(stored) != canonical_json_bytes(identity):
            raise IntegrityError("catalog workspace was staged by a different build")
        self.staged_state = self._get(("staged",))
        self.cursor_state = None if self.staged_state is not None else self._get(("cursor",))
        state = self.staged_state if self.staged_state is not None else self.cursor_state
        self.point = CatalogResumePoint(
            indexed=self._get(("indexed",)) is not None,
            after=None if state is None else state["after"],
            selected_count=0 if state is None else int(state["selectedCount"]),
        )
        return self.point

    def input_loaded(self, source_index: int) -> bool:
        return self._commit is not None and self._get(("input", str(source_index))) is not None

    def mark_input(self, source_index: int, logical_id: str) -> None:
        self._mark(
            ("input", str(source_index)),
            {"sourceIndex": source_index, "logicalId": logical_id},
        )

    def mark_indexed(self) -> None:
        self._mark(("indexed",), {"indexed": True})

    def mark_cursor(self, state: Mapping[str, Any]) -> None:
        self._mark(("cursor",), state)

    def mark_staged(self, state: Mapping[str, Any]) -> None:
        self._mark(("staged",), state)


class _CatalogPolicyInputs:
    """Validate each selected source once and account for the complete universe."""

    def __init__(
        self,
        sources: Sequence[SourceNativeRecordSource],
        descriptions: Sequence[SourceNativeDescription],
        universe_inputs: Sequence[SourceInputSelector],
        workspace: CatalogPolicyWorkspace,
        policy: SourceCatalogPolicy | None = None,
        ledger: "_ResumeLedger | None" = None,
    ) -> None:
        self._policy = policy
        self._ledger = ledger if ledger is not None else _ResumeLedger(workspace)
        self._sources = tuple(sources)
        self._descriptions = tuple(descriptions)
        self._universe_inputs = tuple(universe_inputs)
        if not self._universe_inputs:
            raise ValueError("catalog policy must declare at least one universe input")
        if len(self._universe_inputs) != len(set(self._universe_inputs)):
            raise ValueError("catalog policy universe inputs must be distinct")
        self._workspace = workspace
        self._loaded = False
        self._opened: collections.Counter[SourceInputSelector] = collections.Counter()
        self._universe_passes = 0
        self._completed: set[SourceInputSelector] = set()

    @property
    def descriptions(self) -> tuple[SourceNativeDescription, ...]:
        return self._descriptions

    @property
    def resume(self) -> CatalogResumePoint:
        return self._ledger.point

    @staticmethod
    def _namespace(selector: SourceInputSelector) -> str:
        digest = sha256_digest(canonical_json_bytes(selector.to_dict()))
        return f"{_SOURCE_ROW_NAMESPACE_PREFIX}{digest}"

    def _load(self) -> None:
        if self._loaded:
            return
        for source_index, (source, description) in enumerate(zip(self._sources, self._descriptions, strict=True)):
            # A resumed build does not read an input it already loaded: the
            # ledger bound this workspace to the same inputs in the same order,
            # so the staged rows are the bytes admission checked, read once,
            # earlier. Keyed by position, not logical id: two inputs may share
            # a logical id (the cross-file fixtures do) and each is its own read.
            if self._ledger.input_loaded(source_index):
                continue
            for record, renditions in _source_rows(source):
                selector = SourceInputSelector(
                    description.source_system_id,
                    description.source_system_version,
                    record["scopeId"],
                    record["schemaName"],
                    record["schemaVersion"],
                )
                namespace = self._namespace(selector)
                incoming = {
                    "sourceIndex": source_index,
                    "record": dict(record),
                    "renditions": [dict(value) for value in renditions],
                }
                try:
                    self._workspace.put(namespace, (record["sourceRecordId"],), incoming)
                except IntegrityError as error:
                    self._resolve_repeat(namespace, selector, incoming, error)
            self._ledger.mark_input(source_index, description.logical_id)
        self._loaded = True

    def _resolve_repeat(
        self,
        namespace: str,
        selector: SourceInputSelector,
        incoming: Mapping[str, Any],
        error: IntegrityError,
    ) -> None:
        """Let the policy own a repeated sourceRecordId, or keep the refusal.

        Reached only when ``put`` has already refused, so a corpus with no
        repeats pays nothing for this: the lookup and the resolution are on the
        exception path, not per row. Measured over the 670 non-Federal-Register
        catalog-A releases, exactly two of 2,221,713 records reach it.

        A policy that does not implement ``resolve_source_record_collision``
        keeps the refusal it has today, unchanged and with the same message.
        The capability is read structurally rather than declared on the
        protocol because absence has to mean "refuse as before" for every
        policy that has not thought about it -- a default on the protocol would
        silently opt them all in.
        """

        resolver = getattr(self._policy, "resolve_source_record_collision", None)
        stored = self._workspace.get(namespace, (incoming["record"]["sourceRecordId"],))
        resolution = None if resolver is None or stored is None else resolver(selector, stored, incoming)
        if resolution is None:
            raise IntegrityError("source-native inputs repeat a sourceRecordId for one policy selector") from error
        owner = dict(resolution.owner)
        discarded = dict(resolution.discarded)
        owner["discardedFilings"] = [
            *owner.get("discardedFilings", ()),
            {
                "reasonCode": resolution.reason_code,
                "reason": resolution.reason,
                "record": discarded["record"],
                "renditions": discarded["renditions"],
            },
        ]
        self._workspace.replace(namespace, (owner["record"]["sourceRecordId"],), owner)

    def _ensure_available(self, selector: SourceInputSelector) -> None:
        if not any(
            description.source_system_id == selector.source_system_id
            and description.source_system_version == selector.source_system_version
            for description in self._descriptions
        ):
            raise IntegrityError("catalog policy source input selector matched no source-native input")

    def _row(self, value: Mapping[str, Any]) -> SourceNativeRow:
        if set(value) - {"discardedFilings"} != {"sourceIndex", "record", "renditions"}:
            raise IntegrityError("catalog policy workspace source row has an invalid closed shape")
        source_index = value["sourceIndex"]
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
            or source_index >= len(self._descriptions)
        ):
            raise IntegrityError("catalog policy workspace source index is invalid")
        record = _mapping(value["record"], "catalog policy workspace source record")
        raw_renditions = value["renditions"]
        if not isinstance(raw_renditions, list):
            raise IntegrityError("catalog policy workspace renditions must be an array")
        renditions = tuple(_mapping(raw, "catalog policy workspace rendition") for raw in raw_renditions)
        # Absent is the overwhelming case -- all but two of 2,221,713 records in
        # the 670 non-Federal-Register catalog-A releases -- so the default has
        # to satisfy the same
        # check a present value does, not merely be falsy.
        discarded = value.get("discardedFilings", [])
        if not isinstance(discarded, list):
            raise IntegrityError("catalog policy workspace discarded filings must be an array")
        return SourceNativeRow(
            self._descriptions[source_index],
            record,
            renditions,
            tuple(_mapping(item, "catalog policy workspace discarded filing") for item in discarded),
        )

    def _rows(
        self,
        selector: SourceInputSelector,
        *,
        after: str | None = None,
    ) -> Iterator[SourceNativeRow]:
        self._ensure_available(selector)
        if self._opened[selector] >= MAX_ORDERED_PASSES:
            raise IntegrityError("catalog policy attempted to read one selected input more than twice")
        self._opened[selector] += 1
        self._load()
        previous: str | None = None
        for value in self._workspace.iter_ordered(self._namespace(selector), after=None if after is None else (after,)):
            row = self._row(value)
            if (
                row.description.source_system_id != selector.source_system_id
                or row.description.source_system_version != selector.source_system_version
                or row.record["scopeId"] != selector.scope_id
                or row.record["schemaName"] != selector.schema_name
                or row.record["schemaVersion"] != selector.schema_version
            ):
                raise IntegrityError("catalog policy workspace returned a row for another selector")
            source_item_id = row.record["sourceRecordId"]
            if previous is not None and _utf16_key(source_item_id) <= _utf16_key(previous):
                raise IntegrityError("catalog policy workspace source rows are not sorted and distinct")
            previous = source_item_id
            yield row
        self._completed.add(selector)

    def iter_universe_rows(self) -> Iterator[SourceNativeRow]:
        if self._universe_passes >= MAX_ORDERED_PASSES:
            raise IntegrityError("catalog policy attempted to read the universe more than twice")
        self._universe_passes += 1
        resume = self._ledger.point
        first_pass = self._universe_passes == 1
        # Accounting is a property of the universe, written by the first
        # ordered pass. A resumed run's first pass starts past the cursor, and
        # whether the rows beyond it are already accounted depends on the
        # policy: a two-pass policy accounted the whole universe in the
        # pre-pass it committed, a one-pass policy accounted exactly the rows
        # it staged. So a resumed pass checks before it writes, and only a
        # resumed pass pays that lookup.
        resuming = resume.indexed or resume.after is not None
        streams = [iter(self._rows(selector, after=resume.after)) for selector in self._universe_inputs]
        heap: list[tuple[bytes, int, SourceNativeRow]] = []
        try:
            for index, stream in enumerate(streams):
                row = next(stream, None)
                if row is not None:
                    heapq.heappush(
                        heap,
                        (_utf16_key(str(row.record["sourceRecordId"])), index, row),
                    )
            previous: str | None = None
            while heap:
                _, index, row = heapq.heappop(heap)
                source_item_id = str(row.record["sourceRecordId"])
                if previous is not None and _utf16_key(source_item_id) <= _utf16_key(previous):
                    raise IntegrityError("catalog policy universe sourceItemId values are not globally distinct")
                previous = source_item_id
                # Accounting records which ids the universe contained, which is
                # a property of the universe rather than of a pass over it. The
                # first pass establishes it; a later ordered read must not
                # write it again, and re-deriving the same rows is exactly what
                # the duplicate-key refusal is for.
                if first_pass and not (
                    resuming and self._workspace.get(_UNIVERSE_ACCOUNTING_NAMESPACE, (source_item_id,)) is not None
                ):
                    self._workspace.put(
                        _UNIVERSE_ACCOUNTING_NAMESPACE,
                        (source_item_id,),
                        {"sourceItemId": source_item_id},
                    )
                yield row
                following = next(streams[index], None)
                if following is not None:
                    heapq.heappush(
                        heap,
                        (
                            _utf16_key(str(following.record["sourceRecordId"])),
                            index,
                            following,
                        ),
                    )
        finally:
            for stream in streams:
                stream.close()

    def iter_lookup_rows(self, selector: SourceInputSelector) -> Iterator[SourceNativeRow]:
        if selector in self._universe_inputs:
            raise IntegrityError("catalog policy lookup input must differ from its universe input")
        yield from self._rows(selector)

    def finish(self) -> None:
        if not self._universe_passes or not set(self._universe_inputs).issubset(self._completed):
            raise IntegrityError("catalog policy did not read every declared universe input")
        # Compares which inputs were opened against which were drained, not how
        # many times each was read: `_opened` counts passes now, and a second
        # ordered pass is permitted. An input opened and abandoned partway is
        # still the defect this catches.
        if set(self._opened) != self._completed:
            raise IntegrityError("catalog policy did not fully consume every selected source input")


def _policy_rows(
    sources: Sequence[SourceNativeRecordSource],
    descriptions: Sequence[SourceNativeDescription],
    policy: SourceCatalogPolicy,
    policy_digest: str,
    workspace: CatalogPolicyWorkspace,
    ledger: _ResumeLedger,
) -> Iterator[SourceCatalogItem]:
    inputs: CatalogPolicyInputs = _CatalogPolicyInputs(
        sources,
        descriptions,
        policy.universe_inputs,
        workspace,
        policy,
        ledger=ledger,
    )
    for item in policy.iter_items(inputs, workspace):
        for interpretation in item.interpretations:
            if (
                interpretation.get("policyId") != policy.policy_id
                or interpretation.get("policyVersion") != policy.policy_version
                or interpretation.get("policyDigest") != policy_digest
            ):
                raise IntegrityError("catalog interpretation differs from the installed policy pin")
        yield item
    inputs.finish()
    universe = workspace.iter_ordered(_UNIVERSE_ACCOUNTING_NAMESPACE)
    output = workspace.iter_ordered(_OUTPUT_ACCOUNTING_NAMESPACE)
    for expected, actual in zip_longest(universe, output):
        if expected != actual:
            raise IntegrityError("catalog policy output does not account for its complete universe")
