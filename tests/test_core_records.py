"""Versioned records against independent examples, refusals and source semantics."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import get_args
from uuid import UUID

import jsonschema_rs
import pytest

from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record, record_schema
from docspec.errors import IntegrityError
from tests.support.core_reference import State, check_provenance, value_key


FIXTURE = json.loads((Path(__file__).parent / "fixtures/core/lifecycle.json").read_text())


def example(field, value):
    """Deep-copy the fixture record whose id field for its kind equals `value`."""
    return deepcopy(next(record for record in FIXTURE["records"]
                         if core.RECORD_ID_FIELDS[record["kind"]] == field and record.get(field) == value))


@pytest.mark.parametrize("raw", FIXTURE["records"], ids=lambda record: next(
    value for key, value in record.items() if key.endswith("_id")
))
def test_lifecycle_records_round_trip_through_the_generated_schema(raw):
    """Every fixture record re-encodes to identical bytes and validates against the generated schema."""
    encoded = encode_record(raw)
    parsed = admit_record(encoded)
    assert parsed.__struct_config__.tag == raw["kind"]
    assert encode_record(parsed) == encoded
    validator = jsonschema_rs.Draft202012Validator(record_schema(), offline=True)
    assert validator.is_valid(json.loads(encoded))


def test_fixture_covers_each_top_level_record_family():
    assert {item["kind"] for item in FIXTURE["records"]} == {
        cls.__struct_config__.tag for cls in get_args(core.CoreRecord)
    }


def test_state_has_known_canonical_bytes_and_no_physical_identity():
    """A state has known canonical bytes, and representations differ only by representation id."""
    state = core.State(format_version=1, state_id="s")
    assert encode_record(state) == b'{"format_version":1,"kind":"state","state_id":"s"}'
    before = core.StateRepresentation(format_version=1, representation_id="before", state_id="s", membership=())
    after = core.StateRepresentation(format_version=1, representation_id="after", state_id="s", membership=())
    assert before.state_id == after.state_id == state.state_id
    assert before.representation_id != after.representation_id


@pytest.mark.parametrize("mutation", [
    {"format_version": 2}, {"format_version": True}, {"state_id": 1}, {"state_id": ""},
    {"extra": 1}, {"kind": "unknown"},
])
def test_fixed_shape_and_version_refusals_agree_with_generated_schema(mutation):
    """Wrong version type or value, non-string ids, extra members and unknown kinds refuse in both encoder and schema."""
    raw = {"format_version": 1, "kind": "state", "state_id": "s", **mutation}
    with pytest.raises(IntegrityError):
        encode_record(raw)
    assert not jsonschema_rs.Draft202012Validator(record_schema(), offline=True).is_valid(raw)


def test_wire_admission_requires_a_version_and_rejects_duplicate_keys():
    """A missing `format_version`, a duplicate key and non-canonical spacing all refuse at admission."""
    with pytest.raises(IntegrityError, match="format_version"):
        admit_record(b'{"kind":"state","state_id":"s"}')
    with pytest.raises(IntegrityError, match="duplicate"):
        admit_record(b'{"format_version":1,"kind":"state","state_id":"s","state_id":"other"}')
    with pytest.raises(IntegrityError, match="canonical"):
        admit_record(b'{ "format_version":1,"kind":"state","state_id":"s"}')


@pytest.mark.parametrize("value", [
    b"bytes", bytearray(b"x"), memoryview(b"x"), {1}, frozenset({1}), date(2026, 9, 13),
    Decimal("1"), UUID(int=1), 1.0, float("nan"), 2**53, -(2**53), "\ud800", {1: "nonstring key"},
    {"nested": b"bytes"},
])
def test_python_values_cannot_silently_change_type_during_encoding(value):
    """Bytes, dates, decimals, UUIDs, integral floats, huge ints, lone surrogates and non-string keys all refuse."""
    record = core.Entity(format_version=1, entity_id="e", entity_type="occurrence", value=core.InlineValue(value=value))
    with pytest.raises(IntegrityError):
        encode_record(record)


def test_retained_bytes_are_independent_of_mutable_python_inputs():
    """Appending to the Python value after encoding does not change the admitted bytes."""
    value = {"nested": [1]}
    record = core.Entity(format_version=1, entity_id="e", entity_type="occurrence", value=core.InlineValue(value=value))
    retained = encode_record(record)
    value["nested"].append(2)
    assert admit_record(retained).value.value == {"nested": [1]}


def test_fixture_distinguishes_member_entity_content_request_and_attempt_identities():
    """The fixture keeps member, entity, content, request and attempt identities distinct across equal values."""
    first = admit_record(encode_record(example("entity_id", "o1")))
    second = admit_record(encode_record(example("entity_id", "o2")))
    assert first.entity_id != second.entity_id and value_key(first.value.value) != value_key(second.value.value)
    root_data = example("representation_id", "p1")["membership"]
    entities = {r["entity_id"]: r["value"]["value"] for r in FIXTURE["records"] if r.get("entity_type") == "occurrence"}
    root = State.root("s1", [(row["member_key"], row["occurrence_id"], entities[row["occurrence_id"]]) for row in root_data])
    changed = root.revise("s2", [
        {"sequence": 0, "member_key": "a", "op": "put", "occurrence_id": "o2"},
    ], {"o2": entities["o2"]})
    checkpoint = example("representation_id", "p2")
    assert {member["member_key"]: member["occurrence_id"] for member in checkpoint["membership"]} == changed.members
    assert checkpoint["revision_id"] == "r1"
    assert root.members == FIXTURE["expected"]["state_members"]["s1"]
    assert changed.members == FIXTURE["expected"]["state_members"]["s2"]
    a1, a2 = (example("entity_id", entity) for entity in ("a1", "a2"))
    assert a1["entity_id"] != a2["entity_id"] and a1["value"] == a2["value"]
    x1, x2 = (example("execution_id", execution) for execution in ("x1", "x2"))
    assert x1["execution_id"] != x2["execution_id"] and x1["request_id"] == x2["request_id"]


def test_reuse_selects_an_exact_result_without_rewriting_generation_or_roles():
    """Reuse names the exact generating execution without inventing derivations or changing output roles."""
    results = [admit_record(encode_record(r)) for r in FIXTURE["records"] if r["kind"] == "result"]
    generated = {event.entity_id: result.execution_id for result in results for event in result.generations}
    assert generated == FIXTURE["expected"]["generating_execution"]
    selection = admit_record(encode_record(example("selection_id", "select-c1")))
    assert selection.selected_result_id == "c1" and selection.request_id == "q2"
    assert selection.target.parent_entity_id == "o2"
    assert example("result_id", "c1")["execution_id"] == "x1"
    assert example("execution_id", "x1")["capture_origin"]["parent_entity_id"] == "o1"
    capture, adoption = (admit_record(encode_record(example("result_id", result))) for result in ("c1", "ra"))
    assert capture.outcome.outputs[0].entity_id == adoption.outcome.outputs[0].entity_id == "a1"
    assert capture.outcome.outputs[0].role == "raw" and adoption.outcome.outputs[0].role == "derived"
    assert adoption.generations == () and adoption.outcome.outputs[0].production == "adopted"
    # No derivation is fabricated merely from a capture's usage and output.
    assert capture.usages and capture.generations and capture.derivations == ()


@pytest.mark.parametrize("outcome", [
    {"status": "success", "value": "empty"}, {"status": "success", "value": "null"},
    {"status": "failed", "error": "source refused"}, {"status": "interrupted"}, {"status": "incomplete"},
])
def test_empty_null_failed_interrupted_and_incomplete_have_explicit_distinct_outcomes(outcome):
    """Each outcome status round-trips with its own value, and failure keeps status and error apart."""
    result = core.Result(format_version=1, result_id="r", execution_id="x", outcome=core.Outcome(**outcome))
    decoded = admit_record(encode_record(result))
    assert decoded.outcome.status == outcome["status"]
    assert decoded.outcome.value == outcome.get("value")


@pytest.mark.parametrize("outcome", [
    {"status": "success"}, {"status": "success", "value": "outputs"},
    {"status": "success", "value": "null", "error": "failed"},
    {"status": "failed"}, {"status": "failed", "value": "empty", "error": "failed"},
    {"status": "interrupted", "value": "null"},
])
def test_missing_outputs_cannot_stand_in_for_success_or_failure(outcome):
    """An absent outputs value, bare success, mixed value/error and bare failure all refuse."""
    with pytest.raises(IntegrityError):
        encode_record({"format_version": 1, "kind": "result", "result_id": "r", "execution_id": "x", "outcome": outcome})


def test_narrow_dependencies_do_not_change_whole_or_state_input_bindings():
    request = core.Request(format_version=1, request_id="q", definition_id="capture", inputs=(
        core.WholeInput(label="whole", entity_id="o1"), core.StateInput(label="state", state_id="s1"),
        core.SelectedInput(label="selected", selected_value_id="url1"),
    ), dependencies=(core.Dependency(label="url", binding_label="whole", selection=core.JsonFields(
        selectors=(core.Field(label="url", pointer="/url"),),
    )),))
    decoded = admit_record(encode_record(request))
    assert decoded.inputs == request.inputs
    assert isinstance(decoded.inputs[0], core.WholeInput)
    assert isinstance(decoded.inputs[1], core.StateInput)
    assert isinstance(decoded.inputs[2], core.SelectedInput)
    for name, expected in FIXTURE["expected"]["retained_request_values"].items():
        raw = example("request_id", name)
        assert [binding.get("entity_id", binding.get("selected_value_id")) for binding in raw["inputs"]] == expected


def test_state_member_origins_and_parent_recovery_are_retained_separately_from_comparison():
    selected = core.SelectedValue(
        format_version=1, selected_value_id="fields", definition=core.StateMembers(
            member_selector=core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),)),
            scope=("missing", "a"), material_keys=False, sort_rule="position-then-key-v1",
        ), origin=core.Origin(parent_entity_id="s1"), value=core.FromParent(), member_origins=(
            core.MemberOrigin(member_key="missing", occurrence_id=None), core.MemberOrigin(member_key="a", occurrence_id="o1"),
        ),
    )
    decoded = admit_record(encode_record(selected))
    assert decoded == selected
    assert decoded.member_origins[0].occurrence_id is None
    assert not decoded.definition.material_keys and isinstance(decoded.value, core.FromParent)


def test_unknown_resource_versions_and_append_only_supplements_preserve_original_descriptions():
    definition = example("definition_id", "capture")
    definition["resources"][0] = {"label": "source", "description": {"version": None}, "certainty": "uncertain"}
    original = encode_record(definition)
    supplement = admit_record(encode_record(example("evidence_id", "supplement")))
    assert supplement.supersedes_evidence_id == "omission"
    assert admit_record(original).resources[0].certainty == "uncertain"
    assert admit_record(original).resources[0].description == {"version": None}


@pytest.mark.parametrize("change", ["missing-value", "bad-pointer", "bad-sequence", "same-occurrence"])
def test_revision_record_refusals(change):
    """A missing patch value, bad JSON pointer, repeated sequence or same-occurrence edit all refuse."""
    revision = example("revision_id", "r1")
    if change == "missing-value":
        del revision["value_edits"][0]["patch"][0]["value"]
    elif change == "bad-pointer":
        revision["value_edits"][0]["patch"][0]["path"] = "/~2"
    elif change == "bad-sequence":
        revision["edits"].append(deepcopy(revision["edits"][0]))
    else:
        revision["value_edits"][0]["result_occurrence_id"] = "o1"
    with pytest.raises(IntegrityError):
        encode_record(revision)


def test_json_patch_extra_members_are_ignored_but_explicit_null_is_a_value():
    """Unknown patch members are ignored while an explicit null stays a present value."""
    revision = example("revision_id", "r1")
    revision["value_edits"][0]["patch"][0]["value"] = None
    decoded = admit_record(encode_record(revision))
    assert decoded.value_edits[0].patch[0]["value"] is None
    assert "comment" in decoded.value_edits[0].patch[0]


@pytest.mark.parametrize("change", ["adopted-generation", "missing-generation", "wrong-usage", "self-dependence", "naive-time"])
def test_result_provenance_cannot_contradict_its_bindings(change):
    """Adoption, a missing generation, an unknown usage, self-dependence and naive timestamps all refuse."""
    result = example("result_id", "rp")
    if change == "adopted-generation":
        result["outcome"]["outputs"][0]["production"] = "adopted"
    elif change == "missing-generation":
        result["generations"] = []
    elif change == "wrong-usage":
        result["derivations"][0]["usage_event_id"] = "unknown"
    elif change == "self-dependence":
        result["derivations"][0]["used_entity_id"] = "o2"
    else:
        result["generations"][0]["happened_at"] = "2026-09-13T00:00:00"
    with pytest.raises(IntegrityError):
        encode_record(result)


def test_fixture_actual_events_have_consistent_order_and_acyclic_derivation():
    """The fixture's own generation/usage times are ordered and its derivation graph is acyclic."""
    generations, usages, derivations = [], [], []
    for result in [r for r in FIXTURE["records"] if r["kind"] == "result"]:
        for field, output in [("generations", generations), ("usages", usages)]:
            for event in result.get(field, []):
                output.append({"entity": event["entity_id"], "activity": result["execution_id"],
                               "position": int(event["happened_at"][17:19])})
        derivations.extend((edge["generated_entity_id"], edge["used_entity_id"]) for edge in result.get("derivations", []))
    check_provenance(generations=generations, usages=usages, derivations=derivations)
