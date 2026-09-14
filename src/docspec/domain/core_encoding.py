"""Versioned Core comparison bytes, using the shared codec and array framer.

Logical origins stay in the retained records. Only identities explicitly made
material enter comparison. A digest is evidence to check, never permission to
reuse an unavailable result or waive dependency adequacy.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.identity import OrderedJsonSequenceDigester, canonical_value_bytes, sha256_digest
from docspec.errors import IntegrityError


ABSENT = object()


def selected_fields(fields: Iterable[tuple[str, Any]]) -> list:
    """Labels preserve definition order; ABSENT differs from present null."""
    result, labels = [], set()
    for label, value in fields:
        if not isinstance(label, str) or not label or label in labels:
            raise IntegrityError("selected fields require distinct nonempty labels")
        labels.add(label)
        result.append([label, "absent"] if value is ABSENT else [label, "present", value])
    return result


def member_bytes(value: Any = ABSENT, *, member_key: str | None = None, entity_id: str | None = None) -> bytes:
    """Encode one selected member, including only its material key/identity."""
    row = ["absent"] if value is ABSENT else ["present", value]
    if member_key is not None:
        if not isinstance(member_key, str):
            raise IntegrityError("material member key must be text")
        row.append(["key", member_key])
    if entity_id is not None:
        if value is ABSENT or not isinstance(entity_id, str) or not entity_id:
            raise IntegrityError("material entity identity requires a present member")
        row.append(["entity", entity_id])
    return canonical_value_bytes(row)


def member_stream_evidence(rows: Iterable[bytes], *, ordered: bool = False) -> core.ComparisonEvidence:
    """Consume admitted member bytes in canonical multiset or declared order.

    Sorting belongs to the batch engine. This check retains only the previous
    member and never sorts, decodes, or materializes the state in Python.
    """
    digester = OrderedJsonSequenceDigester(prefix=("docspec-selected-members", 1))
    previous = None
    for row in rows:
        if not ordered and previous is not None and previous > row:
            raise IntegrityError("unordered member comparison requires canonical byte order")
        digester.accept_admitted_payload(row)
        previous = row
    return core.ComparisonEvidence(codec="members-v1", digest=digester.finish(), byte_size=digester.byte_size)


def json_evidence(value: Any, *, entity_id: str | None = None) -> core.ComparisonEvidence:
    data = canonical_value_bytes(value)
    return core.ComparisonEvidence(codec="json-v1", digest=sha256_digest(data), byte_size=len(data), entity_id=entity_id)


def content_evidence(content: core.ContentRef, *, entity_id: str | None = None) -> core.ComparisonEvidence:
    """Physical locator is immaterial; the content owner verifies availability."""
    value = record_value(content, core.ContentRef)
    return core.ComparisonEvidence(codec=value["codec"], digest=value["digest"], byte_size=value["byte_size"],
                                   entity_id=entity_id)


def selector_value(selector: core.Selector) -> dict[str, Any]:
    value = record_value(selector, core.Selector)
    if value["kind"] == "state_members" and value["scope"] is not None:
        value["scope"].sort(key=canonical_value_bytes)
    return value


def correspondence_bytes(
    definition: core.OperationDefinition,
    dependencies: Mapping[str, tuple[core.Selector, core.ComparisonEvidence]],
) -> bytes:
    """Effective operation meaning and uniquely labeled dependency evidence.

    Logical definition, request, execution and origin IDs are intentionally
    absent. Resource uncertainty remains part of the effective description.
    """
    operation = record_value(definition, core.OperationDefinition)
    for key in ("kind", "format_version", "definition_id"):
        del operation[key]
    operation["resources"] = {resource["label"]: {
        key: value for key, value in resource.items() if key != "label"
    } for resource in operation["resources"]}
    bindings = {}
    for label, (selector, evidence) in dependencies.items():
        if not isinstance(label, str) or not label:
            raise IntegrityError("dependency label must be nonempty text")
        selection = selector_value(selector)
        encoded_evidence = record_value(evidence, core.ComparisonEvidence)
        if isinstance(selector, core.StateMembers):
            if evidence.codec != "members-v1" or evidence.entity_id is not None:
                raise IntegrityError("member comparison needs member evidence without a parent identity")
        elif (selector.comparison == "identity") != (evidence.entity_id is not None):
            raise IntegrityError("comparison evidence must match the declared identity mode")
        bindings[label] = {"definition": selection, "evidence": encoded_evidence}
    return canonical_value_bytes({
        "purpose": "docspec-correspondence", "encoding_version": 1,
        "operation": operation, "dependencies": bindings,
    })
