"""RFC 6902 patch semantics are pinned against the spec examples and an independent sequence model.

Every published value edit must record real provenance or fail cleanly: batch edits bound input count,
instruction bytes, prefetch and expansion before any attempt, and a failed patch records a failed result
with no partial entity outputs.
"""

from contextlib import ExitStack
from copy import deepcopy
from functools import partial

import pytest
from hypothesis import given, strategies as st

from docspec.application.core_edits import VALUE_EDIT_BATCH_ROWS, prepare_revision, prepare_value_edit, prepare_value_edits
from docspec.adapters.execution import bounded_map
from docspec.domain import core
from docspec.domain.core_encoding import ABSENT
from docspec.domain.identity import canonical_value_bytes
from docspec.domain.json_values import apply_patch, pointer_value
from docspec.errors import IntegrityError, LimitExceededError
from tests.support.core_strategies import json_values
from tests.test_core_execution import setup, definition, request, progress


@pytest.mark.parametrize("source,patch,expected", [
    ({"foo": "bar"}, [{"op": "add", "path": "/baz", "value": "qux"}], {"foo": "bar", "baz": "qux"}),
    ({"foo": ["bar", "baz"]}, [{"op": "add", "path": "/foo/1", "value": "qux"}], {"foo": ["bar", "qux", "baz"]}),
    ({"foo": ["bar", "qux", "baz"]}, [{"op": "remove", "path": "/foo/1"}], {"foo": ["bar", "baz"]}),
    ({"baz": "qux", "foo": "bar"}, [{"op": "replace", "path": "/baz", "value": "boo"}], {"baz": "boo", "foo": "bar"}),
    ({"foo": {"bar": "baz", "waldo": "fred"}, "qux": {"corge": "grault"}},
     [{"op": "move", "from": "/foo/waldo", "path": "/qux/thud"}],
     {"foo": {"bar": "baz"}, "qux": {"corge": "grault", "thud": "fred"}}),
    (["all", "grass", "cows", "eat"], [{"op": "move", "from": "/1", "path": "/3"}], ["all", "cows", "eat", "grass"]),
    ({"foo": ["bar"]}, [{"op": "add", "path": "/foo/-", "value": ["abc", "def"]}], {"foo": ["bar", ["abc", "def"]]}),
    ({"a/b": {"~key": 1}}, [{"op": "replace", "path": "/a~1b/~0key", "value": None}], {"a/b": {"~key": None}}),
    ({"x": 1}, [{"op": "copy", "from": "", "path": "/copy"}], {"x": 1, "copy": {"x": 1}}),
    ({"x": 1}, [{"op": "copy", "from": "/x", "path": ""}], 1),
    ([1], [{"op": "replace", "path": "", "value": True, "ignored": {"path": "bad"}}], True),
    (None, [{"op": "remove", "path": ""}, {"op": "add", "path": "", "value": []}], []),
    (None, [{"op": "move", "from": "", "path": ""}], None),
    ({"": 0, "01": 1}, [{"op": "test", "path": "/01", "value": 1}, {"op": "remove", "path": "/"}], {"01": 1}),
])
def test_known_patch_results(source, patch, expected):
    before, instructions = deepcopy(source), deepcopy(patch)
    assert canonical_value_bytes(apply_patch(source, patch)) == canonical_value_bytes(expected)
    assert source == before and patch == instructions


@pytest.mark.parametrize("source,patch", [
    ({"x": True}, [{"op": "test", "path": "/x", "value": 1}]),
    ({"x": [True]}, [{"op": "test", "path": "/x", "value": [1]}]),
    ({}, [{"op": "replace", "path": "/x", "value": 1}, {"op": "add", "path": "/x", "value": 1}]),
    ({}, [{"op": "add", "path": "/x/y", "value": 1}]),
    ({}, [{"op": "remove", "path": "/x"}]),
    ({}, [{"op": "move", "from": "/x", "path": "/x"}]),
    ({"x": {}}, [{"op": "move", "from": "/x", "path": "/x/y"}]),
    ([], [{"op": "add", "path": "/01", "value": 1}]),
    ([1], [{"op": "test", "path": "/-", "value": 1}]),
    ([1], [{"op": "remove", "path": "/١"}]),
    ([1], [{"op": "remove", "path": "/" + "9" * 5000}]),
    (None, [{"op": "remove", "path": ""}]),
    ({}, [{"op": "add", "path": "/x"}]),
    ({}, [{"op": "add", "path": "/~2", "value": 1}]),
    ({}, [{"op": "add", "path": "/x", "value": 1.5}]),
    ({}, [{"op": "add", "path": "/x", "value": 2**53}]),
    ({}, [{"op": [], "path": ""}]),
    ({}, [None]),
    ({}, {}),
])
def test_invalid_patch_refuses_without_mutation(source, patch):
    before = deepcopy(source)
    with pytest.raises(IntegrityError):
        apply_patch(source, patch)
    assert source == before


def test_python_aliases_and_copy_never_mutate_another_json_location():
    shared = {"value": 1}
    source = {"a": shared, "b": shared}
    result = apply_patch(source, [{"op": "replace", "path": "/a/value", "value": 2},
                                  {"op": "copy", "from": "/a", "path": "/c"},
                                  {"op": "replace", "path": "/c/value", "value": 3}])
    assert result == {"a": {"value": 2}, "b": {"value": 1}, "c": {"value": 3}}
    assert shared == {"value": 1}


def test_selection_uses_the_same_addresses_with_explicit_absence():
    value = {"a/b": [None, True, 1, "1"], "~1": 8}
    assert pointer_value(value, "/a~1b/0") is None
    assert pointer_value(value, "/a~1b/1") is True
    assert type(pointer_value(value, "/a~1b/2")) is int
    assert pointer_value(value, "/a~1b/3") == "1"
    assert pointer_value(value, "/~01") == 8
    for pointer in ("/absent", "/a~1b/01", "/a~1b/-", "/a~1b/9", "/a~1b/0/x"):
        assert pointer_value(value, pointer) is ABSENT
    with pytest.raises(IntegrityError):
        pointer_value(value, "/absent/~2")


@given(st.lists(json_values, max_size=12), st.lists(st.tuples(st.integers(0, 100), json_values), max_size=20))
def test_ordered_array_changes_match_independent_sequence_model(initial, changes):
    expected, patch = list(initial), []
    for position, value in changes:
        index = position % (len(expected) + 1)
        patch.append({"op": "add", "path": f"/{index}", "value": value})
        expected = expected[:index] + [value] + expected[index:]
        if position % 2:
            target = position % len(expected)
            patch.append({"op": "remove", "path": f"/{target}"})
            expected = expected[:target] + expected[target + 1:]
    assert canonical_value_bytes(apply_patch(initial, patch)) == canonical_value_bytes(expected)


def test_value_edit_fuses_and_records_real_provenance_without_changing_source(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        with operations.publisher.session() as session:
            def source(context):
                context.generate(core.InlineValue(value={"items": [1, None]}), label="source", entity_type="occurrence")
            original = operations.prepare(definition(), request(), source, session=session)
            source_id = original.result.outcome.outputs[0].entity_id
            patch = [{"op": "replace", "path": "/items/0", "value": True}]
            edited, evidence = prepare_value_edit(operations, source_id, patch, upstream=(original,), session=session)
            operations.publish((edited,), session=session)
        assert evidence.source_occurrence_id == source_id
        assert evidence.result_occurrence_id != source_id
        assert evidence.execution_id == edited.execution.execution_id
        before, after = [row.value for batch in ledger.read_records([("entity", source_id), ("entity", evidence.result_occurrence_id)]) for row in batch]
        assert before.value.value == {"items": [1, None]}
        assert after.value.value == {"items": [True, None]}
        assert after.entity_type == "occurrence"
        edge = edited.result.derivations[0]
        assert edge.generation_event_id == edited.result.generations[0].event_id
        assert edge.usage_event_id == edited.result.usages[0].event_id
        saved_request = next(ledger.read_records([("request", edited.execution.request_id)]))[0].value
        saved_definition = next(ledger.read_records([("operation_definition", saved_request.definition_id)]))[0].value
        assert saved_definition.configuration == {"patch": patch}


def test_failed_value_edit_records_failure_and_never_publishes_partial_output(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def source(context):
            context.generate(core.InlineValue(value={"x": 1}), label="source", entity_type="occurrence")
        original = operations.run(definition(), request(), source)
        source_id = original.outcome.outputs[0].entity_id
        with pytest.raises(IntegrityError, match="test failed"):
            prepare_value_edit(operations, source_id, [{"op": "replace", "path": "/x", "value": 2},
                                                      {"op": "test", "path": "/x", "value": 3}])
        with ledger._transaction() as connection:
            failed_id = connection.execute("SELECT record_id FROM records WHERE kind='result' AND outcome='failed'").fetchone()[0]
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity'").fetchone() == (1,)
        failure = next(ledger.read_records([("result", failed_id)]))[0]
        assert not failure.retained and failure.value.generations == ()
        assert failure.value.usages[0].entity_id == source_id
        assert progress(ledger, failure.value.execution_id)[-1]["status"] == "failed"


def test_independent_value_edits_share_the_bounded_worker_and_publication_owners(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def source(context):
            context.generate(core.InlineValue(value={"x": 0}), label="source", entity_type="occurrence")
        original = operations.run(definition(), request(), source)
        source_id = original.outcome.outputs[0].entity_id
        def edit(number):
            prepared, evidence = prepare_value_edit(operations, source_id, [{"op": "replace", "path": "/x", "value": number}])
            operations.publish((prepared,))
            return evidence
        worker = partial(bounded_map, max_workers=2, max_in_flight=2)
        changed = list(worker(edit, range(5)))
        rows = [row.value for batch in ledger.read_records([("entity", item.result_occurrence_id) for item in changed]) for row in batch]
        assert sorted(row.value.value["x"] for row in rows) == list(range(5))
        assert len({item.execution_id for item in changed}) == 5


def test_batch_edits_read_original_values_and_qualify_each_derivation(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        with operations.publisher.session() as session:
            original = operations.prepare(definition(), request(), lambda context: (
                context.generate(core.InlineValue(value={"items": [0]}), label="source", entity_type="occurrence"), None)[1], session=session)
            source = original.result.outcome.outputs[0].entity_id
            patches = [
                [{"op": "add", "path": "/items/-", "value": 1}, {"op": "replace", "path": "/items/0", "value": 2}],
                [{"op": "test", "path": "/items/0", "value": 0}, {"op": "add", "path": "/items/-", "value": 3}],
            ]
            prepared, edits = prepare_value_edits(operations, ((source, patch) for patch in patches), upstream=(original,), session=session)
            patches[0][0]["value"] = 99
            operations.publish((prepared,), session=session)
        assert len({edit.execution_id for edit in edits}) == 1
        assert len({edit.result_occurrence_id for edit in edits}) == 2
        assert edits[0].patch[0]["value"] == 1
        rows = [row.value for batch in ledger.read_records([("entity", source), *(('entity', edit.result_occurrence_id) for edit in edits)]) for row in batch]
        assert [row.value.value for row in rows] == [{"items": [0]}, {"items": [2, 1]}, {"items": [0, 3]}]
        result = prepared.result
        assert [output.label for output in result.outcome.outputs] == ["edited:0", "edited:1"]
        assert len(result.usages) == len(result.generations) == len(result.derivations) == 2
        for edit, usage, generation, edge in zip(edits, result.usages, result.generations, result.derivations, strict=True):
            assert usage.entity_id == edge.used_entity_id == source
            assert generation.entity_id == edge.generated_entity_id == edit.result_occurrence_id
            assert edge.usage_event_id == usage.event_id and edge.generation_event_id == generation.event_id


def test_batch_precondition_failure_records_actual_reads_without_partial_outputs(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        original = operations.run(definition(), request(), lambda context: (
            context.generate(core.InlineValue(value={"x": 1}), label="source", entity_type="occurrence"), None)[1])
        source = original.outcome.outputs[0].entity_id
        with pytest.raises(IntegrityError, match="test failed"):
            prepare_value_edits(operations, (
                (source, [{"op": "replace", "path": "/x", "value": 2}]),
                (source, [{"op": "test", "path": "/x", "value": 2}]),
            ))
        with ledger._transaction() as connection:
            failed_id = connection.execute("SELECT record_id FROM records WHERE kind='result' AND outcome='failed'").fetchone()[0]
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity'").fetchone() == (1,)
        failure = next(ledger.read_records([("result", failed_id)]))[0]
        assert not failure.retained and failure.value.generations == ()
        assert [usage.entity_id for usage in failure.value.usages] == [source, source]
        assert progress(ledger, failure.value.execution_id)[-1]["status"] == "failed"


def test_batch_input_count_is_bounded_before_starting_an_attempt(tmp_path):
    consumed = []
    def edits():
        for index in range(VALUE_EDIT_BATCH_ROWS + 10):
            consumed.append(index)
            yield "source", []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        with pytest.raises(LimitExceededError):
            prepare_value_edits(operations, edits())
        assert len(consumed) == VALUE_EDIT_BATCH_ROWS + 1
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records").fetchone() == (0,)


def test_batch_expansion_limit_records_failure_before_generating_outputs(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        original = operations.run(definition(), request(), lambda context: (
            context.generate(core.InlineValue(value={"body": "x" * 150_000}), label="source", entity_type="occurrence"), None)[1])
        source = original.outcome.outputs[0].entity_id
        # Repeated independent edits expand one small input beyond 8 MiB.
        with pytest.raises(LimitExceededError, match="output exceeds"):
            prepare_value_edits(operations, ((source, []) for _ in range(VALUE_EDIT_BATCH_ROWS)))
        with ledger._transaction() as connection:
            failed_id = connection.execute("SELECT record_id FROM records WHERE kind='result' AND outcome='failed'").fetchone()[0]
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity'").fetchone() == (1,)
        failure = next(ledger.read_records([("result", failed_id)]))[0]
        assert not failure.retained and failure.value.generations == ()
        assert progress(ledger, failure.value.execution_id)[-1]["status"] == "failed"


def test_batch_instruction_bytes_are_bounded_before_starting_an_attempt(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        patch = [{"op": "replace", "path": "", "value": "x" * 150_000}]
        with pytest.raises(LimitExceededError, match="instructions exceed"):
            prepare_value_edits(operations, (("source", patch) for _ in range(VALUE_EDIT_BATCH_ROWS)))
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records").fetchone() == (0,)


def test_batch_input_prefetch_limit_preserves_sources_without_asserting_usage(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        sources = []
        for _ in range(2):
            original = operations.run(definition(), request(), lambda context: (
                context.generate(core.InlineValue(value="x" * 4_300_000), label="source", entity_type="occurrence"), None)[1])
            sources.append(original.outcome.outputs[0].entity_id)
        with pytest.raises(LimitExceededError, match="input prefetch"):
            prepare_value_edits(operations, ((source, []) for source in sources))
        with ledger._transaction() as connection:
            failed_id = connection.execute("SELECT record_id FROM records WHERE kind='result' AND outcome='failed'").fetchone()[0]
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity'").fetchone() == (2,)
        assert all(row.retained for batch in ledger.read_records(("entity", source) for source in sources) for row in batch)
        failure = next(ledger.read_records([("result", failed_id)]))[0]
        assert not failure.retained and failure.value.generations == failure.value.usages == ()
        assert progress(ledger, failure.value.execution_id)[-1]["status"] == "failed"
        def sequential_windows(context):
            for source in sources:
                context.prefetch_entities((source,))
                assert context.read_value(source) == "x" * 4_300_000
        result = operations.prepare(definition(), request("bounded-windows", tuple(
            core.WholeInput(label=str(index), entity_id=source) for index, source in enumerate(sources))), sequential_windows).result
        assert [usage.entity_id for usage in result.usages] == sources
        assert result.outcome.outputs == () and result.generations == ()


@pytest.mark.parametrize("count,body_bytes,cache_limit", [(32, 140_000, None), (1, 80_000, 120_000)])
def test_revision_subdivides_large_source_and_result_read_windows(tmp_path, monkeypatch, count, body_bytes, cache_limit):
    from docspec.application import core_execution
    from docspec.runtime import CoreWorkspace

    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("root", ((str(index), {"body": "x" * body_bytes, "changed": False}) for index in range(count)))
        sources, edits = list(workspace.rows("root")), []
        for offset in range(0, count, 16):
            prepared, group = prepare_value_edits(workspace.operations, ((entity.entity_id,
                [{"op": "replace", "path": "/changed", "value": True}]) for _, entity in sources[offset:offset + 16]))
            workspace.operations.publish((prepared,))
            edits.extend(group)
        if cache_limit is not None:
            monkeypatch.setattr(core_execution, "BATCH_BYTES", cache_limit)
        revision = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
            edits=tuple(core.Put(sequence=index, member_key=sources[index][0], occurrence_id=edit.result_occurrence_id)
                        for index, edit in enumerate(edits)), value_edits=tuple(edits))
        result = workspace.operations.publish((prepare_revision(workspace.operations, revision),))[0]
        assert len(result.usages) == count * 2 + 2
    with CoreWorkspace(tmp_path) as workspace:
        assert len(list(workspace.rows("changed"))) == count
        assert all(entity.value.value == {"body": "x" * body_bytes, "changed": True} for _, entity in workspace.rows("changed"))
        assert all(entity.value.value["changed"] is False for _, entity in workspace.rows("root"))
