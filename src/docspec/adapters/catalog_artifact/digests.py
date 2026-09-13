"""Canonical row details and framed digests shared by both derivation engines."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from rulespec_artifacts import (
    FramedSection,
    framed_section_digest,
)

from docspec.adapters.catalog_artifact.accounting import _DispositionTally, _refuse_collapsed_joins
from docspec.adapters.catalog_artifact.rules import _text, _utf16_key
from docspec.adapters.framing import (
    FramedSectionHasher as _FramedSectionHasher,
)
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    SourceNativeDescription,
)


def _framed_digest(domain: str, name: str, count: int, records: Iterable[object]) -> str:
    try:
        return framed_section_digest(domain, (FramedSection(name, count, records),))
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"cannot compute {domain}: {error}") from error


def requested_universe_set_digest(
    count: int,
    sorted_source_item_ids: Iterable[str],
) -> str:
    """Digest one bounded, UTF-16-ordered requested-universe identity stream."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("requested-universe count must be a non-negative integer")

    def records() -> Iterator[Mapping[str, str]]:
        previous: str | None = None
        for raw_identity in sorted_source_item_ids:
            identity = _text(raw_identity, "requested-universe sourceItemId")
            if previous is not None and _utf16_key(identity) <= _utf16_key(previous):
                raise IntegrityError("requested-universe identities must be sorted and distinct")
            previous = identity
            yield {"sourceItemId": identity}

    return _framed_digest("docspec-requested-universe-set/1", "members", count, records())


def selected_source_set_digest(
    count: int,
    sorted_members: Iterable[tuple[str, str]],
) -> str:
    """Digest one bounded, UTF-16-ordered selected source/document stream."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("selected-source count must be a non-negative integer")

    def records() -> Iterator[Mapping[str, str]]:
        previous: tuple[bytes, bytes] | None = None
        for raw_source_item_id, raw_document_id in sorted_members:
            source_item_id = _text(raw_source_item_id, "selected-source sourceItemId")
            document_id = _text(raw_document_id, "selected-source documentId")
            key = (_utf16_key(source_item_id), _utf16_key(document_id))
            if previous is not None and key <= previous:
                raise IntegrityError("selected-source members must be sorted and distinct")
            previous = key
            yield {"sourceItemId": source_item_id, "documentId": document_id}

    return _framed_digest("docspec-selected-source-set/1", "members", count, records())


def _source_system_set_digest(descriptions: Sequence[SourceNativeDescription]) -> str:
    rows = tuple(
        sorted(
            (
                {
                    "sourceSystemId": value.source_system_id,
                    "sourceSystemVersion": value.source_system_version,
                    "logicalDigest": value.logical_id.rsplit(":", 1)[-1],
                    "sourceStateScope": value.source_state_scope,
                    "sourceStateDigest": value.source_state_digest,
                    "sourceNativeSchemaSetDigest": value.source_native_schema_set_digest,
                }
                for value in descriptions
            ),
            key=lambda value: (
                _utf16_key(value["sourceSystemId"]),
                _utf16_key(value["sourceSystemVersion"]),
                _utf16_key(value["logicalDigest"]),
            ),
        )
    )
    keys = [(value["sourceSystemId"], value["sourceSystemVersion"], value["logicalDigest"]) for value in rows]
    if len(keys) != len(set(keys)):
        raise IntegrityError("source-native inputs contain a duplicate logical source system")
    return _framed_digest("docspec-source-system-set/1", "sources", len(rows), rows)


def _source_schema_set_digest(descriptions: Sequence[SourceNativeDescription]) -> str:
    rows = tuple(
        sorted(
            (
                {
                    "sourceSystemId": value.source_system_id,
                    "sourceSystemVersion": value.source_system_version,
                    "sourceNativeSchemaSetDigest": value.source_native_schema_set_digest,
                }
                for value in descriptions
            ),
            key=lambda value: (
                _utf16_key(value["sourceSystemId"]),
                _utf16_key(value["sourceSystemVersion"]),
                _utf16_key(value["sourceNativeSchemaSetDigest"]),
            ),
        )
    )
    return _framed_digest("docspec-source-native-schema-set/1", "schemas", len(rows), rows)


@dataclass(frozen=True, slots=True)
class _DerivedCatalog:
    """Every digest and diagnostic one derivation pass proves about the rows.

    ``diagnostics`` holds ``joinCoverage`` plus the six
    ``_DIAGNOSTIC_DIGEST_FIELDS`` fingerprints; see that tuple's comment for
    why those six are not published-member references.
    """

    catalog_state_digest: str
    requested_universe_set_digest: str
    selected_source_set_digest: str
    disposition_counts: dict[str, int]
    reason_counts: list[dict[str, object]]
    diagnostics: dict[str, object]
    #: Actual ``path`` (serial, parallel, or serial-fallback) and ``workers``.
    #: Fallback preserves digests but changes timing and memory measurements.
    derivation: Mapping[str, object] = dataclass_field(default_factory=dict)


def _fixed_count_digests(item_count: int, selected_count: int) -> tuple[_FramedSectionHasher, ...]:
    """The six digest headers whose record counts the catalog already declares."""
    return (
        _FramedSectionHasher("docspec-source-catalog-state/1", "sourceItems", item_count),
        _FramedSectionHasher("docspec-requested-universe-set/1", "members", item_count),
        _FramedSectionHasher("docspec-selected-source-set/1", "members", selected_count),
        _FramedSectionHasher("docspec-catalog-dispositions/1", "records", item_count),
        _FramedSectionHasher("docspec-catalog-reasons/1", "records", item_count),
        _FramedSectionHasher("docspec-catalog-rendition-choices/1", "records", item_count),
    )


def _detail_count_digests(
    normalized_count: int, joined_count: int, interpretation_count: int
) -> tuple[_FramedSectionHasher, ...]:
    """Headers delayed until the first pass has counted the variable-size details."""
    return (
        _FramedSectionHasher("docspec-catalog-normalized-fields/1", "records", normalized_count),
        _FramedSectionHasher("docspec-catalog-joined-fields/1", "records", joined_count),
        _FramedSectionHasher("docspec-catalog-interpretations/1", "records", interpretation_count),
    )


def _finish_derivation(
    fixed_digests: tuple[_FramedSectionHasher, ...],
    detail_digests: tuple[_FramedSectionHasher, ...],
    tally: _DispositionTally,
    join_counts: Mapping[str, Mapping[str, int]],
    derivation: Mapping[str, object],
) -> _DerivedCatalog:
    """Assemble one result after either engine has supplied the same ordered bytes."""
    state, requested, selected, dispositions, reasons, rendition_choices = fixed_digests
    normalized, joined, interpretations = detail_digests
    join_coverage = [{"joinId": join_id, **join_counts[join_id]} for join_id in sorted(join_counts, key=_utf16_key)]
    _refuse_collapsed_joins(join_coverage)
    return _DerivedCatalog(
        state.digest(),
        requested.digest(),
        selected.digest(),
        tally.dispositions,
        tally.reason_counts(),
        {
            "joinCoverage": join_coverage,
            "normalizedFieldsDigest": normalized.digest(),
            "joinedFieldsDigest": joined.digest(),
            "dispositionsDigest": dispositions.digest(),
            "reasonsDigest": reasons.digest(),
            "interpretationsDigest": interpretations.digest(),
            "renditionChoicesDigest": rendition_choices.digest(),
        },
        derivation,
    )


def _item_interpretations(item_dict: Mapping[str, Any], kind: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(value for value in item_dict["interpretations"] if value["interpretationKind"] == kind)


def _normalized_field_records_for(row: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    fields: list[Mapping[str, Any]] = []
    for interpretation in _item_interpretations(row, "normalization"):
        fields.extend(interpretation["result"]["fields"])
    previous_key: tuple[bytes, int] | None = None
    for field in sorted(fields, key=lambda value: _utf16_key(value["normalizedField"])):
        field_path = field["normalizedField"]
        for value_index, value in _indexed_values(field["value"]):
            key = (_utf16_key(field_path), value_index)
            if previous_key is not None and key <= previous_key:
                raise IntegrityError("normalized-field diagnostic keys must be ordered and distinct")
            previous_key = key
            yield {
                "sourceItemId": row["sourceItemId"],
                "fieldPath": field_path,
                "valueIndex": value_index,
                "value": value,
                "diagnostics": {
                    "outcome": field["outcome"],
                    "sourcePaths": field["sourcePaths"],
                    "unparseableValues": field["unparseableValues"],
                    "valueSource": field["valueSource"],
                },
            }


def _indexed_values(value: object) -> Iterator[tuple[int, object]]:
    if isinstance(value, list) and value:
        yield from enumerate(value)
        return
    yield 0, value


def _joined_field_records_for(row: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    joins: list[Mapping[str, Any]] = []
    for interpretation in _item_interpretations(row, "exact-join"):
        joins.extend(interpretation["result"]["joins"])
    previous_key: tuple[bytes, bytes, int] | None = None
    for join in sorted(joins, key=lambda value: _utf16_key(value["joinId"])):
        key = (_utf16_key(join["joinId"]), _utf16_key("matchedSourceRecordId"), 0)
        if previous_key is not None and key <= previous_key:
            raise IntegrityError("joined-field diagnostic keys must be ordered and distinct")
        previous_key = key
        yield {
            "sourceItemId": row["sourceItemId"],
            "joinId": join["joinId"],
            "outputPath": "matchedSourceRecordId",
            "valueIndex": 0,
            "value": join["matchedSourceRecordId"],
            "outcome": join["outcome"],
            "evidence": {
                "lookupScopeId": join["lookupScopeId"],
                "sourceField": join["sourceField"],
                "sourceValue": join["sourceValue"],
            },
        }


def _interpretation_records_for(row: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for interpretation in row["interpretations"]:
        by_kind.setdefault(interpretation["interpretationKind"], []).append(interpretation)
    for kind in sorted(by_kind, key=_utf16_key):
        for index, interpretation in enumerate(by_kind[kind]):
            yield {
                "sourceItemId": row["sourceItemId"],
                "interpretationKind": kind,
                "interpretationId": f"{index:04d}",
                "value": interpretation["result"],
                "diagnostics": {
                    "inputScopeIds": interpretation["inputScopeIds"],
                    "policyDigest": interpretation["policyDigest"],
                    "policyId": interpretation["policyId"],
                    "policyVersion": interpretation["policyVersion"],
                },
            }


def _rendition_choice_record(row: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = _item_interpretations(row, "rendition-preference")
    if len(choices) != 1:
        raise IntegrityError("source-catalog row requires one rendition-preference interpretation")
    return {
        "sourceItemId": row["sourceItemId"],
        "selectedFamilyId": choices[0]["result"]["selectedFamilyId"],
        "candidateIds": [candidate["renditionId"] for candidate in row["candidateRenditions"]],
    }
