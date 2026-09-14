"""Actual provenance admission against retained history and an independent oracle."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings, strategies as st
import pytest

from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.domain.core import Derivation, EntityEvent, Execution, Outcome, Result
from docspec.domain.core_admission import event_instant
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch
from tests.support.core_reference import check_provenance


EXECUTION = Execution(format_version=1, execution_id="activity", request_id="request")


def outcome(identity, *, generations=(), usages=(), edges=()):
    return Result(format_version=1, result_id=identity, execution_id="activity",
                  outcome=Outcome(status="success", value="empty"),
                  generations=tuple(generations), usages=tuple(usages), derivations=tuple(edges))


def event(identity, entity, seconds=None):
    return EntityEvent(event_id=identity, entity_id=entity,
                       happened_at=None if seconds is None else f"2026-01-01T00:00:{seconds:02d}Z")


def commit(ledger, result):
    return ledger.commit(MetadataBatch(result.result_id, records=(EXECUTION, result), retained=(("result", result.result_id),)))


def test_provenance_survives_reopen_and_conflicting_generation_rolls_back(tmp_path):
    path = tmp_path / "ledger.sqlite"
    original = outcome("first", generations=(event("g", "entity", 2),))
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        assert commit(ledger, original)
    with closing(LocalSqliteCoreLedger(path, create=False)) as ledger:
        with pytest.raises(IntegrityError, match="conflicting generation"):
            commit(ledger, outcome("conflict", generations=(event("other", "entity", 3),)))
        assert list(ledger.read_records([("result", "conflict")])) == [(None,)]
        assert commit(ledger, outcome("same-established-event", generations=(event("g", "entity", 2),)))


@pytest.mark.parametrize("generation_first", [False, True])
def test_usage_before_generation_refuses_in_either_admission_order(tmp_path, generation_first):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        generation = outcome("generation", generations=(event("g", "e", 5),))
        usage = outcome("usage", usages=(event("u", "e", 3),))
        first, second = (generation, usage) if generation_first else (usage, generation)
        commit(ledger, first)
        with pytest.raises(IntegrityError, match="usage precedes"):
            commit(ledger, second)


def test_partial_stream_entities_allow_usage_before_the_completed_artifact(tmp_path):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        commit(ledger, outcome("stream", generations=(event("chunk-created", "chunk", 1), event("complete-created", "complete", 5)),
                               usages=(event("chunk-used", "chunk", 2),)))
        with pytest.raises(IntegrityError, match="usage precedes"):
            commit(ledger, outcome("false-completion", usages=(event("complete-used", "complete", 2),)))


def test_qualified_derivation_respects_actual_usage_time(tmp_path):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        invalid = outcome("qualified", generations=(event("g", "out", 3),), usages=(event("u", "in", 4),), edges=(
            Derivation(generated_entity_id="out", used_entity_id="in", generation_event_id="g", usage_event_id="u"),
        ))
        with pytest.raises(IntegrityError, match="qualified input usage"):
            commit(ledger, invalid)


def test_cycle_and_transitive_time_constraints_include_retained_edges(tmp_path):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        commit(ledger, outcome("old", generations=(event("ga", "a", 5), event("gc", "c", 3)), edges=(
            Derivation(generated_entity_id="c", used_entity_id="b"),
        )))
        with pytest.raises(IntegrityError, match="ancestors"):
            commit(ledger, outcome("new", edges=(Derivation(generated_entity_id="b", used_entity_id="a"),)))
        # A refused edge was rolled back, so this different acyclic edge succeeds.
        commit(ledger, outcome("new", edges=(Derivation(generated_entity_id="a", used_entity_id="b"),)))
        with pytest.raises(IntegrityError, match="cycle"):
            commit(ledger, outcome("cycle", edges=(Derivation(generated_entity_id="b", used_entity_id="c"),)))


def test_later_generation_claim_checks_transitive_retained_order(tmp_path):
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        commit(ledger, outcome("edges", generations=(event("ga", "a", 5),), edges=(
            Derivation(generated_entity_id="b", used_entity_id="a"),
            Derivation(generated_entity_id="c", used_entity_id="b"),
        )))
        with pytest.raises(IntegrityError, match="ancestors"):
            commit(ledger, outcome("later-evidence", generations=(event("gc", "c", 3),)))


def test_instants_compare_offsets_without_rounding_away_stated_order():
    assert event_instant("2026-01-01T01:00:00+01:00") == event_instant("2026-01-01T00:00:00Z")
    assert event_instant("2026-01-01T00:00:00.000001Z") == event_instant("2026-01-01T00:00:00Z") + 1
    with pytest.raises(IntegrityError, match="precision"):
        event_instant("2026-01-01T00:00:00.0000001Z")


@settings(max_examples=50, deadline=None)
@given(
    edges=st.sets(st.tuples(st.integers(0, 5), st.integers(0, 5)), max_size=10),
    times=st.lists(st.one_of(st.none(), st.integers(0, 9)), min_size=6, max_size=6),
)
def test_graph_admission_agrees_with_independent_small_oracle(edges, times):
    generations = [dict(entity=str(index), activity="activity", event_id=f"g{index}", position=position) for index, position in enumerate(times)]
    pairs = [(str(child), str(parent)) for child, parent in edges]
    try:
        check_provenance(generations=generations, usages=[], derivations=pairs)
        valid = True
    except ValueError:
        valid = False
    with TemporaryDirectory() as directory, closing(LocalSqliteCoreLedger(Path(directory) / "ledger.sqlite")) as ledger:
        record = outcome("test", generations=tuple(event(f"g{index}", str(index), value) for index, value in enumerate(times)),
                         edges=tuple(Derivation(generated_entity_id=child, used_entity_id=parent) for child, parent in pairs))
        if valid:
            assert commit(ledger, record)
        else:
            with pytest.raises(IntegrityError):
                commit(ledger, record)
