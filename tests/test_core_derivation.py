"""Keyed state derivation binds one batch of rows and retained inputs to one operation."""

from hashlib import sha256

import pytest

from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes, stable_urn
from docspec.errors import IntegrityError, StaleBaseError
from docspec.runtime import CoreWorkspace
from tests.support.iceberg_records import files


def values(workspace, state_id):
    """Map member keys to their decoded values for one state."""
    return {key: entity.value.value for key, entity in workspace.rows(state_id)}


def definition(workspace=None, *, implementation="docspec.metadata", configuration=None):
    """Build a caller-supplied operation definition for derive."""
    configuration = {} if configuration is None else configuration
    return core.OperationDefinition(format_version=1,
        definition_id=stable_urn("core-derive-definition", [implementation, configuration]),
        implementation_id=implementation, implementation_version="1", operation_kind="transformation",
        configuration=configuration)


def lookup_binding(workspace, state_id, member_key):
    """Bind one retained occurrence value as a whole input."""
    entity = dict(workspace.rows(state_id))[member_key]
    return core.WholeInput(label=f"lookup:{member_key}", entity_id=entity.entity_id), entity


def result_for(workspace, request_id):
    """Return the retained successful result of the last execution of a request."""
    executions = [identity for group in workspace.ledger.executions(request_id) for identity in group]
    assert executions
    row = next(workspace.ledger.read_records([("result", executions[-1] + ":result")]))[0]
    assert row is not None and row.available
    return row.value


def test_initial_derive_streams_one_state_without_a_base(tmp_path):
    """Without a base, rows stream into one fresh keyed state; retry returns the same state."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("a", 1)])
        binding, entity = lookup_binding(workspace, "source", "a")
        rows = [("one", {"title": "first"}), ("two", None)]
        state = workspace.derive(rows, batch_id="initial", definition=definition(), inputs=(binding,))
        assert values(workspace, state.state_id) == {"one": {"title": "first"}, "two": None}
        request_id = stable_urn("core-derive", "initial") + ":request"
        assert len([item for group in workspace.ledger.executions(request_id) for item in group]) == 1
        result = result_for(workspace, request_id)
        assert result.outcome.outputs[0].entity_id == state.state_id
    with CoreWorkspace(tmp_path) as workspace:
        assert workspace.derive(rows, batch_id="initial", definition=definition(), inputs=(binding,)) == state


def test_initial_derive_records_provenance_to_exact_input_pins(tmp_path):
    """A derived state's result records usage of the rows state and each retained lookup pin."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("a", 1), ("b", 2)])
        rows_binding, rows_entity = lookup_binding(workspace, "source", "a")
        state = workspace.derive([("one", 1)], batch_id="initial", definition=definition(),
                                 inputs=(rows_binding,))
        result = result_for(workspace, stable_urn("core-derive", "initial") + ":request")
        request = next(workspace.ledger.read_records([("request", stable_urn("core-derive", "initial") + ":request")]))[0].value
        rows_state_id = next(item.state_id for item in request.inputs if item.label == "rows")
        used = {event.entity_id for event in result.usages}
        assert rows_state_id in used and rows_entity.entity_id in used
        generated = {event.entity_id for event in result.generations}
        assert state.state_id in generated
        derived = {edge.used_entity_id for edge in result.derivations}
        assert derived == {rows_state_id, rows_entity.entity_id}
        assert all(edge.generated_entity_id == state.state_id for edge in result.derivations)


def test_incremental_derive_shares_base_files_and_preserves_untouched_members(tmp_path):
    """With a base, puts and removals edit the base through one revision that shares its files."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("unchanged", 1), ("update", {"title": "old"}), ("drop", 2)])
        with workspace.publisher.session() as session:
            base_files = files(workspace.records, workspace.states.layers(session, "base")["entities"])
        state = workspace.derive([("new", None), ("update", {"title": "new"})], batch_id="revision",
                                 definition=definition(), inputs=(), base_state_id="base", removals=("drop",))
        assert values(workspace, "base") == {"unchanged": 1, "update": {"title": "old"}, "drop": 2}
        assert values(workspace, state.state_id) == {"unchanged": 1, "update": {"title": "new"}, "new": None}
        with workspace.publisher.session() as session:
            new_files = files(workspace.records, workspace.states.layers(session, state.state_id)["entities"])
            assert all(item in new_files for item in base_files)
        assert workspace.compare("base", state.state_id)["counts"] == {"added": 1, "changed": 1, "removed": 1}


def test_removals_only_revision_removes_without_rows(tmp_path):
    """A base derive with only removals publishes an empty rows state and one remove edit."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("keep", 1), ("drop", 2)])
        state = workspace.derive([], batch_id="remove-only", definition=definition(), inputs=(),
                                 base_state_id="base", removals=("drop",))
        assert values(workspace, state.state_id) == {"keep": 1}


def test_derive_advances_dataset_only_from_the_supplied_base(tmp_path):
    """Dataset promotion advances the current pointer and refuses a stale base."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        workspace.maintenance.select_current("initial", "data", ("state", "base"), None)
        state = workspace.derive([("new", 2)], batch_id="batch", definition=definition(), inputs=(),
                                 base_state_id="base", dataset="data")
        assert workspace.ledger.current("data") == ("state", state.state_id)
        workspace.create("other", [("other", 3)])
        workspace.maintenance.select_current("other", "data", ("state", "other"), ("state", state.state_id))
        with pytest.raises(StaleBaseError):
            workspace.derive([("third", 4)], batch_id="stale", definition=definition(), inputs=(),
                             base_state_id="base", dataset="data")
        assert workspace.ledger.current("data") == ("state", "other")


@pytest.mark.parametrize("change", ["rows", "base", "input", "removals", "order"])
def test_batch_identity_refuses_different_input(tmp_path, change):
    """Reusing a batch id with changed rows, base, inputs, removals or order refuses."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        workspace.create("source", [("a", 1), ("b", 2)])
        lookup, _ = lookup_binding(workspace, "source", "a")
        other, _ = lookup_binding(workspace, "source", "b")
        rows = [("a", 1), ("b", "1")]
        kwargs = {"base_state_id": "base", "removals": ("old",)}
        state = workspace.derive(rows, batch_id="batch", definition=definition(), inputs=(lookup,), **kwargs)
        rows_again, kwargs_again, inputs_again = list(rows), dict(kwargs), (lookup,)
        if change == "rows":
            rows_again[0] = ("a", "1")
        elif change == "base":
            kwargs_again["base_state_id"] = state.state_id
        elif change == "input":
            inputs_again = (other,)
        elif change == "removals":
            kwargs_again["removals"] = ()
        else:
            rows_again.reverse()
        with pytest.raises(IntegrityError, match="batch ID"):
            workspace.derive(rows_again, batch_id="batch", definition=definition(), inputs=inputs_again, **kwargs_again)


def test_retry_with_a_changed_dataset_refuses_at_promotion(tmp_path):
    """Recovering the same batch under a different dataset cannot advance an unmoved pointer."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        state = workspace.derive([("new", 2)], batch_id="batch", definition=definition(), inputs=(),
                                 base_state_id="base")
        with pytest.raises(StaleBaseError):
            workspace.derive([("new", 2)], batch_id="batch", definition=definition(), inputs=(),
                             base_state_id="base", dataset="data")
        assert workspace.ledger.current("data") is None
        assert workspace.derive([("new", 2)], batch_id="batch", definition=definition(), inputs=(),
                                base_state_id="base") == state


def test_unavailable_lookup_input_refuses_before_rows_are_published(tmp_path):
    """A derive whose caller input is missing fails its attempt without retaining any rows state."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        with pytest.raises(IntegrityError, match="derive input is unavailable"):
            workspace.derive([("new", 2)], batch_id="batch", definition=definition(),
                             inputs=(core.WholeInput(label="lookup", entity_id="urn:missing"),),
                             base_state_id="base")
        request_id = stable_urn("core-derive", "batch") + ":request"
        executions = [identity for group in workspace.ledger.executions(request_id) for identity in group]
        assert len(executions) == 1
        failure = next(workspace.ledger.read_records([("result", executions[0] + ":result")]))[0]
        assert failure.value.outcome.status == "failed"
        states = [row.value.state_id for group in workspace.ledger.retained_records(kind="state") for row in group]
        assert states == ["base"]


def test_derive_without_rows_or_removals_refuses(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        with pytest.raises(IntegrityError, match="at least one row or removal"):
            workspace.derive([], batch_id="empty", definition=definition(), inputs=(), base_state_id="base")
        with pytest.raises(IntegrityError, match="at least one row or removal"):
            workspace.derive([], batch_id="empty-initial", definition=definition(), inputs=())


def test_removals_and_dataset_require_a_base(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        with pytest.raises(IntegrityError, match="require a base"):
            workspace.derive([("a", 1)], batch_id="no-base-removal", definition=definition(), inputs=(),
                             removals=("a",))
        with pytest.raises(IntegrityError, match="require a base"):
            workspace.derive([("a", 1)], batch_id="no-base-dataset", definition=definition(), inputs=(),
                             dataset="data")


@pytest.mark.parametrize("inputs,error", [
    ((core.StateInput(label="rows", state_id="urn:base"),), "distinct and avoid"),
    ((core.StateInput(label="base", state_id="urn:base"),), "distinct and avoid"),
    ((core.StateInput(label="a", state_id="urn:one"), core.StateInput(label="a", state_id="urn:two")), "distinct and avoid"),
    ((core.SelectedInput(label="a", selected_value_id="urn:selected"),), "must be state or whole-input"),
])
def test_invalid_input_bindings_refuse(tmp_path, inputs, error):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        with pytest.raises(IntegrityError, match=error):
            workspace.derive([("a", 1)], batch_id="batch", definition=definition(), inputs=inputs, base_state_id="base")


@pytest.mark.parametrize("rows,removals,error", [
    ([("a", 1), ("a", 2)], (), "duplicate member key"),
    ([(1, "bad-key")], (), "must be strings"),
    ([("bad", 1.5)], (), "JSON codec"),
    ([("a", 1)], ("a",), "repeats a put"),
])
def test_invalid_rows_and_removals_refuse_and_close_the_stream(tmp_path, rows, removals, error):
    closed = []
    def source():
        try:
            yield from rows
        finally:
            closed.append(True)
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        with pytest.raises((IntegrityError, ValueError), match=error):
            workspace.derive(source(), batch_id="bad", definition=definition(), inputs=(),
                             base_state_id="base", removals=removals)
        assert closed == [True]
        assert not list(workspace.ledger.executions(stable_urn("core-derive", "bad") + ":request"))


def test_duplicate_removal_keys_refuse(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        with pytest.raises(IntegrityError, match="distinct member keys"):
            workspace.derive([("a", 1)], batch_id="bad", definition=definition(), inputs=(),
                             base_state_id="base", removals=("a", "a"))


def test_derive_streams_more_than_one_metadata_batch(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        state = workspace.derive(((str(i), "x" * 4096) for i in range(2049)), batch_id="bulk",
                                 definition=definition(), inputs=(), base_state_id="base")
        assert workspace.compare("base", state.state_id, sample_limit=0)["counts"] == {"added": 2049, "changed": 0, "removed": 0}


def test_retry_recovers_after_interruption(tmp_path, monkeypatch):
    """Retrying the same batch resumes from the durable prefix the interrupted boundary left."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        workspace.maintenance.select_current("initial", "data", ("state", "base"), None)
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError("simulated interruption")
            patch.setattr(workspace.operations, "_publish_journal", fail)
            with pytest.raises(OSError, match="simulated"):
                workspace.derive([("new", 2)], batch_id="batch", definition=definition(), inputs=(),
                                 base_state_id="base", dataset="data")
        assert workspace.ledger.current("data") == ("state", "base")
    with CoreWorkspace(tmp_path) as workspace:
        state = workspace.derive([("new", 2)], batch_id="batch", definition=definition(), inputs=(),
                                 base_state_id="base", dataset="data")
        assert values(workspace, state.state_id) == {"old": 1, "new": 2}
        assert workspace.ledger.current("data") == ("state", state.state_id)


def test_upsert_preserves_its_definition_request_and_occurrence_identities(tmp_path):
    """upsert delegates to derive but keeps its stable definition, request and occurrence identity scheme."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("base", [("old", 1)])
        rows = [("new", {"title": "value"})]
        state = workspace.upsert("base", rows, batch_id="daily")
        request_id = stable_urn("core-upsert", "daily") + ":request"
        request = next(workspace.ledger.read_records([("request", request_id)]))[0].value
        assert request.definition_id == stable_urn("core-upsert-definition",
            {"batch_id": "daily",
             "rows_digest": "sha256:" + sha256(b"".join(canonical_value_bytes([key, value]) + b"\n"
                                                        for key, value in rows)).hexdigest(),
             "row_count": 1, "dataset": None})
        entity = dict(workspace.rows(state.state_id))["new"]
        assert entity.entity_id == stable_urn("core-upsert-occurrence", ["daily", "new"])
        assert len([item for group in workspace.ledger.executions(request_id) for item in group]) == 1
