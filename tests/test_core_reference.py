"""Known answers precede production Core algorithms; no conformance claim yet."""

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from hypothesis import given
import pytest

from tests.support.core_reference import State, check_provenance, selected_field, selected_value, value_key
from tests.support.core_strategies import json_values, keyed_roots, membership_histories


CASES = json.loads((Path(__file__).parent / "fixtures/core/selected-values.json").read_text())
URL = [{"label": "url", "pointer": "/url"}]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_selected_field_known_answers(case):
    if "error" in case:
        with pytest.raises(ValueError, match=case["error"]):
            selected_field(case["value"], case["pointer"])
    else:
        actual = selected_field(case["value"], case["pointer"])
        assert value_key(actual) == value_key(case["expected"])


def test_presence_and_types_have_distinct_comparison_keys():
    values = [["absent"], *(["present", v] for v in (None, False, 0, 1, "1", "", [], {}))]
    assert len({value_key(value) for value in values}) == 9


def test_labeled_composites_preserve_order_and_field_absence():
    fields = [{"label": "b", "pointer": "/b"}, {"label": "a", "pointer": "/a"}]
    assert selected_value({"a": None}, fields) == [["b", "absent"], ["a", "present", None]]
    with pytest.raises(ValueError, match="duplicate selector"):
        selected_value({}, [fields[0], fields[0]])


def test_array_positions_are_reevaluated_against_each_immutable_parent():
    """Array positions resolve against the parent they were evaluated on, so a shifted list yields the old element."""
    assert selected_field(["old", "next"], "/1") == ["present", "next"]
    assert selected_field(["new", "old", "next"], "/1") == ["present", "old"]


def test_title_revision_preserves_url_correspondence_and_original_state():
    """A title-only revision keeps URL selection equal while whole-value and material-entity selections differ."""
    base = State.root("s1", [("a", "e1", {"url": "u", "title": "old"})])
    changed = base.revise("s2", [{"sequence": 0, "member_key": "a", "op": "put", "occurrence_id": "e2"}], {
        "e2": {"url": "u", "title": "new"},
    })
    assert changed.members == {"a": "e2"}
    assert base.members == {"a": "e1"}
    assert base.occurrences == {"e1": {"url": "u", "title": "old"}}
    assert base.selected_members(selectors=URL) == changed.selected_members(selectors=URL)
    assert base.selected_members() != changed.selected_members()
    assert base.selected_members(selectors=URL, material_entities=True) != changed.selected_members(
        selectors=URL, material_entities=True,
    )


def test_equal_root_values_remain_distinct_occurrences_and_duplicates():
    """Two equal values stay distinct occurrences, and reusing one occurrence id under two keys refuses."""
    root = State.root("s", [("a", "e1", 1), ("b", "e2", 1)])
    assert root.members == {"a": "e1", "b": "e2"}
    assert root.selected_members() == [["present", ["present", 1]], ["present", ["present", 1]]]
    with pytest.raises(ValueError, match="distinct keys and occurrences"):
        State.root("bad", [("a", "e1", 1), ("b", "e1", 1)])


def test_named_member_absence_differs_from_missing_field_and_null():
    root = State.root("s", [("b", "e1", {}), ("c", "e2", {"url": None})])
    assert root.selected_members(selectors=URL, scope=["c", "a", "b"]) == [
        ["absent"], ["present", [["url", "absent"]]], ["present", [["url", "present", None]]],
    ]
    with pytest.raises(ValueError, match="duplicate requested"):
        root.selected_members(scope=["a", "a"])


def test_material_keys_and_consumed_order_change_comparison():
    """Material keys and consumed key order change comparison even when the multiset is equal."""
    root = State.root("s1", [("a", "e1", 1), ("b", "e2", 2)])
    swapped = root.revise("s2", [
        {"sequence": 0, "member_key": "a", "op": "put", "occurrence_id": "e2"},
        {"sequence": 1, "member_key": "b", "op": "put", "occurrence_id": "e1"},
    ])
    assert Counter(map(value_key, root.selected_members())) == Counter(map(value_key, swapped.selected_members()))
    assert root.selected_members(material_keys=True) != swapped.selected_members(material_keys=True)
    assert root.selected_members(ordered_keys=["a", "b"]) != root.selected_members(ordered_keys=["b", "a"])


@pytest.mark.parametrize("edits", [
    [{"sequence": 0, "member_key": "missing", "op": "remove"},
     {"sequence": 1, "member_key": "missing", "op": "put", "occurrence_id": "e"}],
    [{"sequence": 0, "member_key": "a", "op": "remove"},
     {"sequence": 0, "member_key": "a", "op": "put", "occurrence_id": "e"}],
    [{"member_key": "a", "op": "remove"}],
    [{"sequence": -1, "member_key": "a", "op": "remove"}],
    [{"sequence": True, "member_key": "a", "op": "remove"}],
    [{"sequence": 0, "member_key": "a", "op": "put", "occurrence_id": "unknown"}],
])
def test_later_edits_cannot_hide_invalid_preconditions(edits):
    root = State.root("s1", [("a", "e", 1)])
    with pytest.raises(ValueError):
        root.revise("s2", edits)
    assert root.members == {"a": "e"}


def test_retained_occurrence_cannot_change_value_or_type():
    """Republishing a retained occurrence with a different value or type refuses."""
    root = State.root("s1", [("a", "e", 1)])
    with pytest.raises(ValueError, match="cannot change"):
        root.revise("s2", [], {"e": True})


@given(keyed_roots())
def test_root_preserves_every_key_occurrence_and_value(rows):
    root = State.root("s", rows)
    assert [(key, entity, value_key(root.occurrences[entity])) for key, entity in root.members.items()] == [
        (key, entity, value_key(value)) for key, entity, value in rows
    ]


@given(membership_histories())
def test_revision_order_is_explicit_and_checkpoint_preserves_effective_state(case):
    """Reversed edits produce the explicit precondition order, and a midpoint checkpoint yields the same final state."""
    initial, edits, expected = case
    root = State("s1", initial.copy(), {"e0": 0, "e1": 1, "e2": 2})
    result = root.revise("s2", list(reversed(edits)))
    assert result.members == expected
    assert root.members == initial
    midpoint = len(edits) // 2
    prefix = root.revise("prefix", edits[:midpoint])
    checkpoint = State(prefix.state_id, deepcopy(prefix.members), deepcopy(prefix.occurrences))
    assert checkpoint.revise("s2", edits[midpoint:]).members == result.members


@given(json_values)
def test_whole_selection_keeps_exact_json_type_and_is_independent(value):
    """Whole selection keeps the exact JSON type and does not track later mutation of the source."""
    selected = selected_field(value, "")
    assert value_key(selected) == value_key(["present", value])
    if isinstance(value, list):
        value.append("mutation")
        assert selected[1] != value


def test_imported_entities_and_repeated_same_generation_assertion_are_valid():
    check_provenance(generations=[], usages=[{"entity": "imported", "activity": "a", "position": 1}], derivations=[])
    gen = {"entity": "e", "activity": "a", "position": 1}
    check_provenance(generations=[gen, gen], usages=[], derivations=[])


def test_returning_existing_entity_does_not_generate_it_again():
    with pytest.raises(ValueError, match="conflicting generation"):
        check_provenance(generations=[
            {"entity": "e", "activity": "a", "position": 1},
            {"entity": "e", "activity": "b", "position": 2},
        ], usages=[], derivations=[])


@pytest.mark.parametrize("relations", [[("a", "a")], [("a", "b"), ("b", "c"), ("c", "a")]])
def test_generation_self_dependence_and_cycles_refuse_without_timestamps(relations):
    with pytest.raises(ValueError, match="self-dependence|cycle"):
        check_provenance(generations=[], usages=[], derivations=relations)


def test_stream_usage_must_name_what_existed_at_the_boundary():
    generations = [
        {"entity": "chunk", "activity": "capture", "position": 1},
        {"entity": "complete", "activity": "capture", "position": 3},
    ]
    check_provenance(generations=generations, usages=[
        {"entity": "chunk", "activity": "parse", "position": 2},
    ], derivations=[])
    with pytest.raises(ValueError, match="usage precedes generation"):
        check_provenance(generations=generations, usages=[
            {"entity": "complete", "activity": "parse", "position": 2},
        ], derivations=[])


def test_generation_usage_allows_same_position_but_derivation_requires_strict_order():
    check_provenance(generations=[{"entity": "e", "activity": "a", "position": 1}], usages=[
        {"entity": "e", "activity": "b", "position": 1},
    ], derivations=[])
    with pytest.raises(ValueError, match="derivation generation order"):
        check_provenance(generations=[
            {"entity": "e1", "activity": "a", "position": 1},
            {"entity": "e2", "activity": "b", "position": 1},
        ], usages=[], derivations=[("e2", "e1")])


def test_observed_events_respect_activity_bounds():
    with pytest.raises(ValueError, match="outside activity"):
        check_provenance(generations=[{"entity": "e", "activity": "a", "position": 4}],
                         usages=[], derivations=[], activities={"a": (0, 3)})


@pytest.mark.parametrize("change", [None, "late-usage", "wrong-source", "other-activity", "unknown-event"])
def test_qualified_derivation_identifies_its_actual_usage_and_generation(change):
    """A qualified derivation must name its actual usage and generation events in the same activity."""
    generation = {"event_id": "g", "entity": "output", "activity": "transform", "position": 2}
    usage = {"event_id": "u", "entity": "input", "activity": "transform", "position": 1}
    relation = {"generated": "output", "used": "input", "generation_event": "g", "usage_event": "u"}
    if change == "late-usage":
        usage["position"] = 3
    elif change == "wrong-source":
        usage["entity"] = "other"
    elif change == "other-activity":
        usage["activity"] = "other"
    elif change == "unknown-event":
        relation["usage_event"] = "unknown"
    arguments = {"generations": [generation], "usages": [usage], "derivations": [], "qualified_derivations": [relation]}
    if change is None:
        check_provenance(**arguments)
    else:
        with pytest.raises(ValueError):
            check_provenance(**arguments)


def test_unknown_intermediate_time_does_not_hide_impossible_derivation_order():
    """An untimed intermediate cannot mask a transitive contradiction between two timed derivations."""
    with pytest.raises(ValueError, match="transitive derivation"):
        check_provenance(generations=[
            {"entity": "first", "activity": "a", "position": 2},
            {"entity": "middle", "activity": "b"},
            {"entity": "last", "activity": "c", "position": 1},
        ], usages=[], derivations=[("middle", "first"), ("last", "middle")])
