"""Exact lookup indexes and preserved discarded-filing evidence."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    SourceInputSelector,
)

from .records import (
    _COMMENT_SCOPE,
    _DOCKET_SCOPE,
    _DOCUMENT_SCOPE,
    _IndexedRow,
)

_DOCKET_INDEX = "regulations-gov-catalog/dockets"

_DOCUMENT_INDEX = "regulations-gov-catalog/document-index"

_FEDERAL_REGISTER_INDEX = "regulations-gov-catalog/federal-register"

#: Where the Federal Register index finds the key the document side looks up by.
#:
#: The document side has only a bare ``frDocNum`` to join on, so the index must
#: hold bare numbers. This was ``sourceRecordId`` until 2026-09-05, and the two
#: were the same string until the producer made ``sourceRecordId`` composite
#: (``00-111@2000-01-14``) so that a reused number stops discarding the older
#: filing. Composite key in, bare key out: 499,238 lookups returned zero
#: matches, the join coverage fell from 430,323 to 0, and the build still
#: reported ``pass``. See DocSpec decision 0003, its Federal Register join-key correction.
#:
#: Keyed on a named field rather than on the record's identity precisely because
#: an identity is allowed to change shape; a join key that rides on one inherits
#: every move it makes.
_FEDERAL_REGISTER_KEY_PATH: Final = ("record", "document_number")


def _lookup_key(record: Mapping[str, Any], key_path: Sequence[str]) -> str | None:
    """Walk a named key path to a nonempty string, returning None when it is absent."""
    value: Any = record
    for step in key_path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(step)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class LookupIndexStats:
    """What the index refused, so the receipt can say so rather than imply it."""

    indexed: int
    ambiguous_keys: int
    ambiguous_rows: int
    unkeyed_rows: int


def _index_rows(
    inputs: CatalogPolicyInputs,
    workspace: CatalogPolicyWorkspace,
    selector: SourceInputSelector | None,
    namespace: str,
    *,
    key_path: Sequence[str],
) -> LookupIndexStats:
    """Index lookup rows by a named field, refusing keys that name several rows.

    A bare number resolving to more than one record cannot be attributed from
    the number alone, so the index holds neither; keeping one (the latest, say)
    would silently re-assert the collapse DocSpec 0003 removed. It makes two
    ordered passes over the input, which ``CatalogPolicyInputs.finish`` permits
    explicitly; memory is the key set, not the corpus, and the refused counts
    go to stderr so the abstention is visible.
    """
    if selector is None:
        return LookupIndexStats(0, 0, 0, 0)

    seen: set[str] = set()
    ambiguous: set[str] = set()
    unkeyed = 0
    for row in inputs.iter_lookup_rows(selector):
        key = _lookup_key(row.record, key_path)
        if key is None:
            unkeyed += 1
            continue
        if key in seen:
            ambiguous.add(key)
        seen.add(key)

    indexed = 0
    ambiguous_rows = 0
    for row in inputs.iter_lookup_rows(selector):
        key = _lookup_key(row.record, key_path)
        if key is None:
            continue
        if key in ambiguous:
            ambiguous_rows += 1
            continue
        workspace.put(
            namespace,
            (key,),
            {
                "record": dict(row.record),
                "renditions": [dict(value) for value in row.renditions],
                **_carried_discards(row),
            },
        )
        indexed += 1

    stats = LookupIndexStats(
        indexed=indexed,
        ambiguous_keys=len(ambiguous),
        ambiguous_rows=ambiguous_rows,
        unkeyed_rows=unkeyed,
    )
    # stderr, in the shape the CLI uses for its own two diagnostics, so the
    # build log records what the index refused. An abstention reports
    # downstream as an ordinary no-match -- the outcome enum is sealed at three
    # values in urn:docspec:schema:source-catalog-item:1.0 -- so without this
    # line the difference between "no such record" and "several, and we will
    # not choose" is unrecoverable from the artifact.
    print(
        json.dumps(
            {
                "format": "docspec-source-catalog-build-diagnostic",
                "formatVersion": "1.0",
                "lookupIndex": {
                    "namespace": namespace,
                    "keyPath": list(key_path),
                    "indexed": stats.indexed,
                    "ambiguousKeys": stats.ambiguous_keys,
                    "ambiguousRowsRefused": stats.ambiguous_rows,
                    "unkeyedRows": stats.unkeyed_rows,
                },
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    return stats


def _indexed_row(
    workspace: CatalogPolicyWorkspace,
    namespace: str,
    source_id: str | None,
) -> _IndexedRow | None:
    if source_id is None:
        return None
    value = workspace.get(namespace, (source_id,))
    if value is None:
        return None
    return _stored_row(value)


def _carried_discards(row: Any) -> dict[str, Any]:
    """Stage filings the loader collapsed into this row, and nothing when there are none.

    Written for both staging paths rather than the universe one alone: the
    lookup path never carries a discarded filing today (the only lookup input,
    Federal Register, has a flat record that ``_record_data`` refuses), but
    staging it keeps that property true when a second lookup input appears.
    """

    if not row.discarded_filings:
        return {}
    return {"discardedFilings": [dict(value) for value in row.discarded_filings]}


def _stored_discards(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Read back what `_carried_discards` staged, refusing a shape it did not write."""

    raw = value.get("discardedFilings", [])
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise IntegrityError("catalog join index discarded filings must be an array of objects")
    return tuple(raw)


def _stored_row(
    value: Mapping[str, Any],
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
    """Return ``(record, renditions)`` from a stored index row, refusing any other shape."""
    # Subtracting the one optional key keeps the shape closed against every
    # other: a row may carry filings the loader collapsed into it, and nothing
    # else. `_carried_discards` writes it and `_stored_discards` reads it, and
    # this guard sits between them -- widened together with them rather than
    # left refusing what its own module had started writing.
    if set(value) - {"discardedFilings"} != {"record", "renditions"} or not isinstance(value["record"], Mapping):
        raise IntegrityError("catalog join index row has an invalid closed shape")
    raw_renditions = value["renditions"]
    if not isinstance(raw_renditions, Sequence) or isinstance(raw_renditions, (str, bytes, bytearray, memoryview)):
        raise IntegrityError("catalog join index renditions must be an array")
    renditions = tuple(value for value in raw_renditions if isinstance(value, Mapping))
    if len(renditions) != len(raw_renditions):
        raise IntegrityError("catalog join index contains a non-object rendition")
    return value["record"], renditions


def _stage_universe(inputs: CatalogPolicyInputs, workspace: CatalogPolicyWorkspace, *, index_documents: bool) -> None:
    """Stage only the indexes this policy reads; inputs own the universe scan.

    Sampling and comment conversion share the document index's stored bytes,
    and neither needs it when both are absent; earlier duplicate staging cost
    4.7 MB per thousand rows. The docket index stays unconditional because
    document conversion joins to it, and an undeclared universe scope is
    refused.
    """

    for row in inputs.iter_universe_rows():
        stored = {
            "record": dict(row.record),
            "renditions": [dict(value) for value in row.renditions],
            **_carried_discards(row),
        }
        source_item_id = str(row.record["sourceRecordId"])
        if row.record["scopeId"] == _DOCUMENT_SCOPE:
            if index_documents:
                workspace.put(_DOCUMENT_INDEX, (source_item_id,), stored)
        elif row.record["scopeId"] == _DOCKET_SCOPE:
            workspace.put(_DOCKET_INDEX, (source_item_id,), stored)
        elif row.record["scopeId"] != _COMMENT_SCOPE:
            raise IntegrityError("Regulations.gov policy received an undeclared universe scope")
