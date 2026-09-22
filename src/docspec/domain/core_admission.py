"""One structural admission and encoding path for versioned Core records.

Wire bytes use the shared canonical JSON format; Python callers may construct
records or supply mappings, and encode_record validates and snapshots them.
Logical immutability is established by those retained bytes, not by freezing a
mutable JSON object inside a Python record; ledger admission owns cross-record
checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import re
from typing import Any

import msgspec

from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.domain.selected_values import validate_fields_value
from docspec.errors import IntegrityError


def pointer_tokens(pointer: str) -> tuple[str, ...]:
    """RFC 6901 syntax validation, before any engine evaluates the address."""
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")) or re.search(r"~(?![01])", pointer):
        raise IntegrityError("invalid JSON Pointer")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:])


def _distinct(values, label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise IntegrityError(f"duplicate {label}: {value!r}")
        seen.add(value)


def _selector(value: core.Selector) -> None:
    if isinstance(value, core.JsonFields):
        _distinct((field.label for field in value.selectors), "selector label")
        for field in value.selectors:
            pointer_tokens(field.pointer)
    elif isinstance(value, core.StateMembers):
        if value.scope is not None:
            _distinct(value.scope, "member key in selection scope")
        _selector(value.member_selector)


def _origin(value: core.Origin) -> None:
    if value.member_key is not None and value.state_id is None:
        raise IntegrityError("member origin requires its state context")


def validate_patch(operations) -> None:
    """The shared RFC 6902 shape rules for revision records and evaluation."""
    if not isinstance(operations, (list, tuple)):
        raise IntegrityError("JSON Patch requires an array of operations")
    for operation in operations:
        if not isinstance(operation, dict):
            raise IntegrityError("JSON Patch operation must be an object")
        op = operation.get("op")
        if not isinstance(op, str) or op not in {"add", "remove", "replace", "move", "copy", "test"}:
            raise IntegrityError("unknown JSON Patch operation")
        path = pointer_tokens(operation.get("path"))
        if op in {"add", "replace", "test"} and "value" not in operation:
            raise IntegrityError(f"JSON Patch {op} requires a value member")
        if op in {"move", "copy"}:
            source = pointer_tokens(operation.get("from"))
            if op == "move" and len(path) > len(source) and path[:len(source)] == source:
                raise IntegrityError("cannot move a value into its descendant")
        # Extra members are deliberately ignored by RFC 6902. Their JSON values
        # still pass the shared codec when the containing record is admitted.


def event_instant(value: str | None) -> int | None:
    """Index stated instants exactly at the profile's microsecond precision."""
    if value is not None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise IntegrityError("invalid provenance event time") from error
        if parsed.utcoffset() is None:
            raise IntegrityError("provenance event time requires a timezone")
        if any(any(digit != "0" for digit in fraction[6:]) for fraction in re.findall(r"[.,](\d+)", value)):
            raise IntegrityError("provenance event time exceeds microsecond precision")
        try:
            utc = parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError) as error:
            raise IntegrityError("provenance event time is outside the UTC date range") from error
        return ((utc.toordinal() * 86400 + utc.hour * 3600 + utc.minute * 60 + utc.second) * 1_000_000 + utc.microsecond)
    return None


def _result(record: core.Result) -> None:
    outcome = record.outcome
    _distinct((binding.label for binding in outcome.outputs), "output binding label")
    if outcome.status == "success":
        if outcome.error is not None or outcome.value is None:
            raise IntegrityError("success requires an explicit outcome and no error")
        if bool(outcome.outputs) != (outcome.value == "outputs"):
            raise IntegrityError("output bindings disagree with the explicit outcome")
    elif outcome.value is not None:
        raise IntegrityError("unsuccessful outcome cannot claim an empty, null, or output success")
    elif outcome.status == "failed" and outcome.error is None:
        raise IntegrityError("failed outcome requires an error")
    _distinct((event.event_id for event in (*record.generations, *record.usages)), "provenance event identity")
    _distinct((event.entity_id for event in record.generations), "generated entity")
    generated = {event.entity_id for event in record.generations}
    for binding in outcome.outputs:
        if binding.production == "new" and binding.entity_id not in generated:
            raise IntegrityError("new output lacks its generation event")
        if binding.production == "adopted" and binding.entity_id in generated:
            raise IntegrityError("adopted output cannot be generated again")
    for event in (*record.generations, *record.usages):
        event_instant(event.happened_at)
    generations = {event.event_id: event for event in record.generations}
    usages = {event.event_id: event for event in record.usages}
    for derivation in record.derivations:
        if derivation.generated_entity_id == derivation.used_entity_id:
            raise IntegrityError("generation self-dependence")
        if derivation.generation_event_id is not None:
            event = generations.get(derivation.generation_event_id)
            if event is None or event.entity_id != derivation.generated_entity_id:
                raise IntegrityError("derivation generation event does not match its entity")
        if derivation.usage_event_id is not None:
            event = usages.get(derivation.usage_event_id)
            if event is None or event.entity_id != derivation.used_entity_id:
                raise IntegrityError("derivation usage event does not match its entity")


def _check(record: core.Fixed) -> core.Fixed:
    if isinstance(record, core.StateRepresentation) and isinstance(record.membership, tuple):
        _distinct((member.member_key for member in record.membership), "state member key")
    elif isinstance(record, core.Revision):
        if record.base_state_id == record.result_state_id:
            raise IntegrityError("revision requires a distinct result state")
        if isinstance(record.edits, tuple):
            _distinct((edit.sequence for edit in record.edits), "edit sequence")
        for edit in record.value_edits:
            if edit.source_occurrence_id == edit.result_occurrence_id:
                raise IntegrityError("value edit requires a new occurrence")
            validate_patch(edit.patch)
    elif isinstance(record, core.SelectedValue):
        _selector(record.definition)
        _origin(record.origin)
        if isinstance(record.definition, core.JsonFields) and isinstance(record.value, core.InlineValue):
            validate_fields_value(record.definition.selectors, record.value.value)
        if isinstance(record.definition, core.JsonFields) and isinstance(record.value, core.ContentRef) and record.value.codec != "json-v1":
            raise IntegrityError("selected JSON fields require the JSON value codec")
        if isinstance(record.member_origins, tuple):
            _distinct((member.member_key for member in record.member_origins), "selected member origin")
        if not isinstance(record.definition, core.StateMembers) and record.member_origins:
            raise IntegrityError("member origins require a state-member selection")
    elif isinstance(record, core.OperationDefinition):
        _distinct((resource.label for resource in record.resources), "resource label")
    elif isinstance(record, core.Request):
        _distinct((binding.label for binding in record.inputs), "input binding label")
        _distinct((dependency.label for dependency in record.dependencies), "dependency label")
        labels = {binding.label for binding in record.inputs}
        for dependency in record.dependencies:
            if dependency.binding_label not in labels:
                raise IntegrityError("dependency names an unknown input binding")
            _selector(dependency.selection)
    elif isinstance(record, core.Execution) and record.capture_origin is not None:
        _origin(record.capture_origin)
    elif isinstance(record, core.Result):
        _result(record)
    elif isinstance(record, core.Selection):
        _origin(record.target)
        _distinct(record.output_labels, "selected output label")
    elif isinstance(record, (core.Whole, core.JsonFields, core.StateMembers)):
        _selector(record)
    return record


def _convert(value: Any, record_type: Any = core.CoreRecord) -> Any:
    try:
        return _check(msgspec.convert(value, type=record_type, strict=True))
    except (msgspec.ValidationError, TypeError, ValueError) as error:
        raise IntegrityError(f"invalid Core record: {error}") from error


def admit_record(data: bytes) -> core.CoreRecord:
    """Decode raw canonical bytes once, preserving duplicate-key evidence."""
    return _convert(decode_canonical_json_value(data, label="Core record"))


@lru_cache(maxsize=128)
def _struct_fields(record_type: type[core.Fixed]):
    # Core schemas are immutable. Cache their descriptions, never record values;
    # the bound also limits retention if a caller supplies additional classes.
    return msgspec.structs.fields(record_type)


def _plain(value: Any) -> Any:
    if isinstance(value, core.Fixed):
        config = value.__struct_config__
        result = {config.tag_field: config.tag} if config.tag is not None else {}
        for field in _struct_fields(type(value)):
            item = getattr(value, field.name)
            # msgspec.to_builtins would silently turn bytes into base64, dates
            # into text, and sets into arrays inside Any payloads. Preserve the
            # value for the shared codec to accept or refuse instead.
            result[field.encode_name] = item if field.type is Any else _plain(item)
        return result
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def encode_record(record: core.CoreRecord | dict[str, Any]) -> bytes:
    """Validate a Python record and create its immutable canonical snapshot."""
    try:
        return canonical_value_bytes(record_value(record))
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"Core record is outside its JSON codec: {error}") from error


def record_value(value: core.Fixed | dict[str, Any], record_type: Any = core.CoreRecord) -> dict[str, Any]:
    """One strict conversion for record encoding and bounded comparison metadata.

    The shared encoder still owns the JSON domain of Any payload values. This
    avoids serializing and decoding metadata merely to inspect its fields.
    """
    return _plain(_convert(_plain(value), record_type))


class AdmittedRecord:
    """Checked record bytes that stay valid across bounded internal handoffs.

    Both input forms pass ordinary admission. The canonical bytes are immutable;
    readers receive detached values so mutation cannot invalidate their snapshot.
    """

    __slots__ = ("_payload",)

    def __init__(self, value: core.CoreRecord | dict[str, Any] | bytes):
        if isinstance(value, bytes):
            payload = value
            admit_record(payload)
        else:
            try:
                payload = canonical_value_bytes(record_value(value))
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"Core record is outside its JSON codec: {error}") from error
        object.__setattr__(self, "_payload", payload)

    def __setattr__(self, name, value):
        raise AttributeError("admitted record snapshots are immutable")

    @property
    def payload(self) -> bytes:
        return self._payload

    @property
    def value(self) -> dict[str, Any]:
        # Entry admission already established canonical bytes and semantics.
        # Native decoding detaches each reader and supplies record defaults,
        # without retaining or recursively copying another full Python tree.
        return _plain(msgspec.json.decode(self._payload, type=core.CoreRecord))


def record_parts(value: core.CoreRecord | dict[str, Any] | AdmittedRecord) -> tuple[dict[str, Any], bytes]:
    """Read a checked value and its original canonical bytes without re-encoding."""
    snapshot = value if type(value) is AdmittedRecord else AdmittedRecord(value)
    return snapshot.value, snapshot.payload


def record_schema() -> dict[str, Any]:
    """Generate the structural schema from the exact admission types."""
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **msgspec.json.schema(core.CoreRecord)}
