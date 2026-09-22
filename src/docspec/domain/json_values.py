"""Typed JSON Pointer lookup and ordered edits over immutable JSON inputs.

Addresses share admission's syntax rule: unresolved selection returns ABSENT,
while an unresolved patch precondition refuses the entire edit. Only changed
values enter this evaluator; membership joins remain in the batch engine.
"""

from copy import deepcopy
import re

from docspec.domain.core_admission import pointer_tokens, validate_patch
from docspec.domain.core_encoding import ABSENT
from docspec.domain.identity import canonical_value_bytes, snapshot_json_value
from docspec.errors import IntegrityError


def _index(token, length, *, insert=False):
    if insert and token == "-":
        return length
    if not re.fullmatch(r"0|[1-9][0-9]*", token) or len(token) > len(str(length)):
        raise IntegrityError("JSON Pointer array index is invalid")
    index = int(token)
    if index > length or (index == length and not insert):
        raise IntegrityError("JSON Pointer array index is out of range")
    return index


def _key(parent, token, *, insert=False):
    if isinstance(parent, dict):
        if not insert and token not in parent:
            raise IntegrityError("JSON Pointer member is missing")
        return token
    if isinstance(parent, list):
        return _index(token, len(parent), insert=insert)
    raise IntegrityError("JSON Pointer cannot traverse a scalar or absent value")


def _lookup(value, tokens):
    for token in tokens:
        value = value[_key(value, token)]
    if value is ABSENT:
        raise IntegrityError("JSON Pointer document is absent")
    return value


def pointer_value(value, pointer):
    """Read an admitted JSON value; preserve null, types and missing members."""
    tokens = pointer_tokens(pointer)
    try:
        return _lookup(value, tokens)
    except IntegrityError:
        return ABSENT


def _write(document, tokens, value, *, add=False):
    if not tokens:
        if not add:
            _lookup(document, ())
        return value
    parent = _lookup(document, tokens[:-1])
    key = _key(parent, tokens[-1], insert=add)
    if add and isinstance(parent, list):
        parent.insert(key, value)
    else:
        parent[key] = value
    return document


def _remove(document, tokens):
    if not tokens:
        _lookup(document, ())
        return ABSENT
    parent = _lookup(document, tokens[:-1])
    del parent[_key(parent, tokens[-1])]
    return document


def apply_patch(value, operations):
    """Return a complete new JSON value or refuse without changing either input.

    A codec round trip removes Python object aliasing as well as validating the
    shared JSON domain. Root removal may be followed by root add, but a completed
    occurrence must contain a value; absence never silently becomes JSON null.
    """
    document = snapshot_json_value(value, label="JSON Patch source")
    operations = snapshot_json_value(operations, label="JSON Patch operations")
    validate_patch(operations)
    for operation in operations:
        op, path = operation["op"], pointer_tokens(operation["path"])
        if op == "test":
            if canonical_value_bytes(_lookup(document, path)) != canonical_value_bytes(operation["value"]):
                raise IntegrityError("JSON Patch test failed")
        elif op == "remove":
            document = _remove(document, path)
        else:
            if op in {"move", "copy"}:
                source = pointer_tokens(operation["from"])
                replacement = deepcopy(_lookup(document, source))
                if op == "move":
                    document = _remove(document, source)
            else:
                replacement = deepcopy(operation["value"])
            document = _write(document, path, replacement, add=op != "replace")
    if document is ABSENT:
        raise IntegrityError("JSON Patch must produce a complete occurrence value")
    return document
