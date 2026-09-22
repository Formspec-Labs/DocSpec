"""Core comparison meaning and known bytes; the engine shares these encoders."""

import hashlib
import json

import pytest

from docspec.domain import core
from docspec.domain.core_encoding import (
    ABSENT, content_evidence, correspondence_bytes, json_evidence, member_bytes,
    member_stream_evidence, selected_fields,
)
from docspec.domain.identity import OrderedJsonSequenceDigester, canonical_value_bytes
from docspec.errors import IntegrityError


def definition(identifier="definition", *, version="1", resources=()):
    """Build a transformation operation definition with the given id, version and resources."""
    return core.OperationDefinition(format_version=1, definition_id=identifier,
                                    implementation_id="extract", implementation_version=version,
                                    operation_kind="transformation", configuration={}, resources=resources)


def test_member_presence_and_material_identity_have_known_bytes():
    """Member bytes distinguish absent, present-null, numbers and strings; duplicate labels and entity-only keys refuse."""
    fields = selected_fields([("missing", ABSENT), ("null", None), ("number", 1), ("string", "1")])
    assert member_bytes(fields) == (
        b'["present",[["missing","absent"],["null","present",null],'
        b'["number","present",1],["string","present","1"]]]'
    )
    assert member_bytes() == b'["absent"]'
    assert member_bytes(member_key="") == b'["absent",["key",""]]'
    assert member_bytes(["present", None], member_key="k", entity_id="e") == (
        b'["present",["present",null],["key","k"],["entity","e"]]'
    )
    with pytest.raises(IntegrityError):
        member_bytes(entity_id="nonexistent")
    with pytest.raises(IntegrityError):
        selected_fields([("x", 1), ("x", 2)])


def test_member_stream_has_known_preimage_and_preserves_duplicate_counts():
    """The stream digest uses the known preimage, keeps duplicates, and refuses unordered payloads unless explicitly ordered."""
    rows = [b'["absent"]', b'["present",["present",1]]', b'["present",["present",1]]']
    expected = b'["docspec-selected-members",1,[["absent"],["present",["present",1]],["present",["present",1]]]]'
    evidence = member_stream_evidence(iter(rows))
    assert evidence.digest == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert evidence.byte_size == len(expected)
    assert evidence.digest != member_stream_evidence(rows[:-1]).digest
    with pytest.raises(IntegrityError, match="canonical byte order"):
        member_stream_evidence(reversed(rows))
    assert member_stream_evidence(reversed(rows), ordered=True).digest != evidence.digest


@pytest.mark.parametrize("prefix", [None, (), ("purpose", 1)])
@pytest.mark.parametrize("mode", ["single", "batch", "mixed"])
@pytest.mark.parametrize("values", [[], [None, {"\U00010000": "\u001f", "\ue000": 1}, "e\u0301", True]])
def test_existing_array_digest_and_prefixed_stream_use_one_framer(prefix, mode, values):
    """Single, batched and mixed payload accepts all digest to the same canonical array framing."""
    expected = canonical_value_bytes(values if prefix is None else [*prefix, values])
    digest = OrderedJsonSequenceDigester(prefix=prefix)
    payloads = [canonical_value_bytes(value) for value in values]
    if mode == "single":
        for payload in payloads:
            digest.accept_admitted_payload(payload)
    else:
        digest.accept_admitted_batch([])
        if mode == "mixed":
            for payload in payloads[:2]:
                digest.accept_admitted_payload(payload)
        else:
            digest.accept_admitted_batch(payloads[:2])
        digest.accept_admitted_batch(())
        digest.accept_admitted_batch(tuple(payloads[2:]))
    assert digest.finish() == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert digest.finish() == digest.finish()  # Finishing cannot append another trailer.
    assert digest.byte_size == len(expected)
    with pytest.raises(RuntimeError):
        digest.accept(None)
    with pytest.raises(RuntimeError):
        digest.accept_admitted_batch([])


@pytest.mark.parametrize("invalid", ["1", bytearray(b"1"), memoryview(b"1"), 1, None])
def test_admitted_batch_refuses_nonbytes_before_changing_digest(invalid):
    """A non-bytes item refuses before appending, so the final digest covers only accepted payloads."""
    digest = OrderedJsonSequenceDigester()
    digest.accept_admitted_payload(b"null")
    with pytest.raises(TypeError):
        digest.accept_admitted_batch([b"true", invalid])
    digest.accept_admitted_batch([b"1"])
    expected = b"[null,1]"
    assert digest.finish() == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert digest.byte_size == len(expected)


@pytest.mark.parametrize("invalid", [None, b"1", "1", iter([b"1"])])
def test_admitted_batch_requires_a_bounded_sequence(invalid):
    digest = OrderedJsonSequenceDigester()
    with pytest.raises(TypeError):
        digest.accept_admitted_batch(invalid)
    assert digest.finish() == "sha256:" + hashlib.sha256(b"[]").hexdigest()
    assert digest.byte_size == 2


def test_definition_identity_is_not_effective_operation_identity():
    """Correspondence bytes ignore `definition_id` and bind configuration, implementation, version and dependencies."""
    assert correspondence_bytes(definition(), {}) == (
        b'{"dependencies":{},"encoding_version":1,"operation":{"configuration":{},'
        b'"implementation_id":"extract","implementation_version":"1",'
        b'"operation_kind":"transformation","resources":{}},"purpose":"docspec-correspondence"}'
    )
    selected = core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),))
    evidence = json_evidence([["url", "present", "https://example.invalid"]])
    left = correspondence_bytes(definition("first"), {"input": (selected, evidence)})
    assert correspondence_bytes(definition("second"), {"input": (selected, evidence)}) == left
    assert correspondence_bytes(definition(version="2"), {"input": (selected, evidence)}) != left
    value = json.loads(left)
    assert value["purpose"] == "docspec-correspondence" and value["encoding_version"] == 1
    assert "definition_id" not in value["operation"]


def test_selection_versions_and_evidence_types_refuse_before_correspondence():
    """A non-current selection version or malformed comparison evidence digest refuses before correspondence bytes exist."""
    with pytest.raises(IntegrityError):
        correspondence_bytes(definition(), {"x": (core.Whole(version=2), json_evidence(1))})
    with pytest.raises(IntegrityError):
        correspondence_bytes(definition(), {"x": (core.Whole(), core.ComparisonEvidence(
            codec="json-v1", digest="not a digest", byte_size=1,
        ))})


def test_resource_uncertainty_and_content_identity_are_explicit():
    """Certainty changes correspondence bytes, and content evidence binds digest and entity id but not locator."""
    a = core.Resource(label="model", description={"version": "1"}, certainty="established")
    b = core.Resource(label="model", description={"version": "1"}, certainty="uncertain")
    assert correspondence_bytes(definition(resources=(a,)), {}) != correspondence_bytes(definition(resources=(b,)), {})
    content = core.ContentRef(digest="sha256:" + hashlib.sha256(b"opaque\xff").hexdigest(), byte_size=7,
                              locator="original", media_type="application/octet-stream")
    relocated = core.ContentRef(digest=content.digest, byte_size=7, locator="moved", media_type=content.media_type)
    assert content_evidence(content) == content_evidence(relocated)
    assert content_evidence(content, entity_id="a") != content_evidence(content, entity_id="b")
    assert content_evidence(content).codec == "bytes-v1"


def test_named_scope_and_dependency_order_do_not_change_meaning():
    """Named scope order and dependency insertion order leave correspondence unchanged; identity comparison still refuses."""
    evidence = member_stream_evidence([])
    a = core.StateMembers(member_selector=core.Whole(), scope=("b", "a"))
    b = core.StateMembers(member_selector=core.Whole(), scope=("a", "b"))
    assert correspondence_bytes(definition(), {"x": (a, evidence)}) == correspondence_bytes(definition(), {"x": (b, evidence)})
    scalar = (core.Whole(), json_evidence(1))
    assert correspondence_bytes(definition(), {"b": scalar, "a": scalar}) == correspondence_bytes(definition(), {"a": scalar, "b": scalar})
    with pytest.raises(IntegrityError):
        correspondence_bytes(definition(), {"x": (core.Whole(comparison="identity"), json_evidence(1))})


@pytest.mark.parametrize("value", [1.0, 2**53, b"not JSON", "\ud800"])
def test_selected_values_cannot_bypass_the_shared_codec(value):
    """Integral floats, huge integers, bytes and lone surrogates cannot enter member bytes."""
    with pytest.raises((TypeError, ValueError)):
        member_bytes(selected_fields([("v", value)]))
