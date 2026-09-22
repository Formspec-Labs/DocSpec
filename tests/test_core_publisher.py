"""Binding-specific retention through the actual content and metadata owners."""

from contextlib import closing
from dataclasses import replace

import pytest

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.application.core_publication import CorePublisher
from docspec.domain import core
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from docspec.ports.core_ledger import MetadataBatch


def entity(identity, value, *, occurrence=False):
    """Build an artifact, or occurrence, entity carrying the given value."""
    return core.Entity(format_version=1, entity_id=identity,
                       entity_type="occurrence" if occurrence else "artifact", value=value)


def definition(*, capture=False):
    """Build a transformation definition, or a capture definition when `capture` is true."""
    return core.OperationDefinition(format_version=1, definition_id="definition", implementation_id="test:operation",
                                    implementation_version="1", operation_kind="capture" if capture else "transformation", configuration={})


def outcome(*, outputs=(), generations=()):
    """Build the `result` outcome whose value states whether outputs were bound."""
    return core.Result(format_version=1, result_id="result", execution_id="execution",
                       outcome=core.Outcome(status="success", value="outputs" if outputs else "empty", outputs=tuple(outputs)),
                       generations=tuple(generations))


def snapshot(ledger, key):
    """Read exactly one record for the key and return it."""
    return next(ledger.read_records([key]))[0]


def test_retention_scope_can_exceed_new_record_count_without_unbounded_bindings(tmp_path, monkeypatch):
    """2100 scoped entities bind in batches no larger than BATCH_ROWS, and a repeated publish is idempotent."""
    from docspec.ports.record_storage import BATCH_ROWS

    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(tmp_path / "blobs"))
        with publisher.session() as session:
            inputs = []
            for group in range(3):
                state = core.State(format_version=1, state_id=f"state-{group}")
                entities = tuple(entity(f"{group}:{i}", core.InlineValue(value=i), occurrence=True) for i in range(700))
                representation = core.StateRepresentation(
                    format_version=1, representation_id=f"representation-{group}", state_id=state.state_id,
                    membership=tuple(core.Membership(member_key=str(i), occurrence_id=value.entity_id)
                                     for i, value in enumerate(entities)),
                )
                session.publish(MetadataBatch(f"import-{group}", records=(*entities, state, representation),
                                              retained=(("state", state.state_id),)))
                inputs.append(core.StateInput(label=str(group), state_id=state.state_id))
            request = core.Request(format_version=1, request_id="request", definition_id="definition", inputs=tuple(inputs),
                                   dependencies=tuple(core.Dependency(label=value.label, binding_label=value.label,
                                                                      selection=core.Whole()) for value in inputs))
            execution = core.Execution(format_version=1, execution_id="execution", request_id="request")
            batch = MetadataBatch("combined", records=(definition(), request, execution, outcome()),
                                  retained=(("result", "result"),))
            sizes = []
            original_open = ledger._open

            class ObservedConnection:
                def __init__(self, connection):
                    self.connection = connection

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def executemany(self, statement, rows):
                    rows = tuple(rows)
                    sizes.append(len(rows))
                    assert len(rows) <= BATCH_ROWS
                    return self.connection.executemany(statement, rows)

            monkeypatch.setattr(ledger, "_open", lambda **kwargs: ObservedConnection(original_open(**kwargs)))
            assert session.publish(batch)
            assert not session.publish(batch)
            required = session.validate(batch)
            assert len(required) > BATCH_ROWS
            assert ("entity", "2:699") in required
            assert max(sizes) == BATCH_ROWS
        assert snapshot(ledger, ("result", "result")).retained


def test_import_complete_inline_state_without_inventing_execution(tmp_path):
    """Importing a complete inline state records no provenance, retries as a no-op, and a closed session refuses."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(tmp_path / "blobs"))
        records = (
            entity("occurrence", core.InlineValue(value={"text": "same"}), occurrence=True),
            core.State(format_version=1, state_id="state"),
            core.StateRepresentation(format_version=1, representation_id="representation", state_id="state", membership=(
                core.Membership(member_key="", occurrence_id="occurrence"),
                core.Membership(member_key="second-key", occurrence_id="occurrence"),
            )),
        )
        batch = MetadataBatch("root", records=records, retained=(("state", "state"),))
        with publisher.session() as session:
            assert session.publish(batch)
        assert snapshot(ledger, ("state", "state")).available
        assert snapshot(ledger, ("entity", "occurrence")).available
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM provenance_events").fetchone() == (0,)
        with publisher.session() as session:
            assert not session.publish(batch)
        with pytest.raises(StateTransitionError, match="closed"):
            session.publish(batch)


def test_complete_state_refuses_missing_values_and_conflicting_representation(tmp_path):
    """A retained state without a membership representation and a second disagreeing representation both refuse."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(tmp_path / "blobs"))
        state = core.State(format_version=1, state_id="s")
        with publisher.session() as session:
            with pytest.raises(IntegrityError, match="membership representation"):
                session.publish(MetadataBatch("state", records=(state,), retained=(("state", "s"),)))
            empty = core.StateRepresentation(format_version=1, representation_id="empty", state_id="s", membership=())
            assert session.publish(MetadataBatch("state", records=(state, empty), retained=(("state", "s"),)))
            changed = core.StateRepresentation(format_version=1, representation_id="changed", state_id="s",
                                               membership=(core.Membership(member_key="k", occurrence_id="o"),))
            with pytest.raises(IntegrityError, match="disagree"):
                session.publish(MetadataBatch("changed", records=(changed,), retained=(("state", "s"),)))


def test_direct_projected_input_does_not_retain_its_parent_and_new_bytes_are_not_reread(tmp_path, monkeypatch):
    """A selected input retains only the selected value, never its parent, and verified bytes are not reread."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
        publisher = CorePublisher(ledger, blobs)
        origin = core.Origin(parent_entity_id="unretained-occurrence", state_id="unretained-state", member_key="key")
        with publisher.session() as session:
            content = session.retain_bytes([b"captured"])
            records = (
                definition(capture=True),
                core.SelectedValue(format_version=1, selected_value_id="url", definition=core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),)),
                                   origin=origin, value=core.InlineValue(value=[["url", "present", "https://example.invalid/"]])),
                core.Request(format_version=1, request_id="request", definition_id="definition",
                             inputs=(core.SelectedInput(label="url", selected_value_id="url"),), dependencies=(core.Dependency(label="url", binding_label="url", selection=core.Whole()),)),
                core.Execution(format_version=1, execution_id="execution", request_id="request", capture_origin=origin),
                entity("artifact", content),
                outcome(outputs=(core.ResultBinding(label="raw", entity_id="artifact", role="raw", production="new"),),
                        generations=(core.EntityEvent(event_id="generation", entity_id="artifact"),)),
            )
            def unexpected(*args, **kwargs):
                raise AssertionError("new, already verified content was reread")
            monkeypatch.setattr(blobs, "read", unexpected)
            monkeypatch.setattr(blobs, "verify", unexpected)
            assert session.publish(MetadataBatch("capture", records=records, retained=(("result", "result"),)))
        assert snapshot(ledger, ("entity", "artifact")).retained
        assert snapshot(ledger, ("entity", "unretained-occurrence")) is None
        assert snapshot(ledger, ("state", "unretained-state")) is None


def test_whole_input_obligation_is_not_reduced_by_narrow_dependency(tmp_path):
    """A whole-input obligation still requires the entity's bytes even when a narrow dependency exists."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
        publisher = CorePublisher(ledger, blobs)
        with publisher.session() as session:
            content = session.retain_bytes([b"whole input"])
        blobs.delete(next(iter(session.ready)))
        records = (
            definition(), entity("input", content),
            core.Request(format_version=1, request_id="request", definition_id="definition", inputs=(core.WholeInput(label="input", entity_id="input"),),
                         dependencies=(core.Dependency(label="url", binding_label="input", selection=core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),))),)),
            core.Execution(format_version=1, execution_id="execution", request_id="request"), outcome(),
        )
        with publisher.session() as session, pytest.raises(IntegrityError):
            session.publish(MetadataBatch("missing-whole", records=records, retained=(("result", "result"),)))
        assert snapshot(ledger, ("result", "result")) is None


def test_opaque_content_cannot_be_relabelled_as_admitted_json(tmp_path):
    """Retaining non-JSON bytes under a `json-v1` codec refuses."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(tmp_path / "blobs"))
        with publisher.session() as session:
            opaque = session.retain_bytes([b"not JSON"])
            forged = core.ContentRef(digest=opaque.digest, byte_size=opaque.byte_size, locator=opaque.locator, media_type=opaque.media_type, codec="json-v1")
            with pytest.raises(IntegrityError, match="canonical JSON"):
                session.publish(MetadataBatch("json", records=(entity("e", forged),), retained=(("entity", "e"),)))


@pytest.mark.parametrize("after_commit", [False, True])
def test_publication_reconciles_lost_commit_response(tmp_path, monkeypatch, after_commit):
    """On reopen a lost commit response is reconciled so the retry is exactly-once either way."""
    path = tmp_path / "ledger.sqlite"
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        publisher = CorePublisher(ledger, blobs)
        commit = ledger.commit
        def interrupted(batch):
            if after_commit:
                commit(batch)
            raise OSError("interrupted commit response")
        with publisher.session() as session:
            content = session.retain_value({"payload": "kept"})
            batch = MetadataBatch("stable-publication", records=(entity("e", content),), retained=(("entity", "e"),))
            monkeypatch.setattr(ledger, "commit", interrupted)
            with pytest.raises(OSError, match="interrupted"):
                session.publish(batch)
    with closing(LocalSqliteCoreLedger(path)) as ledger:
        assert ledger.is_committed(batch.unit_id) is after_commit
        with CorePublisher(ledger, blobs).session() as session:
            assert session.publish(batch) is not after_commit
            assert not session.publish(batch)
        assert snapshot(ledger, ("entity", "e")).available


def test_missing_root_reports_admission_error(tmp_path):
    """A retained key whose required selection is missing reports an admission error."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        with CorePublisher(ledger, LocalContentAddressedBlobStore(tmp_path / "blobs")).session() as session:
            with pytest.raises(IntegrityError, match="required selection"):
                session.publish(MetadataBatch("missing", retained=(("selection", "absent"),)))


def test_reuse_retains_only_selected_outputs_and_requested_inputs(tmp_path, monkeypatch):
    """Reuse keeps only selected outputs and requested inputs, rechecks evidence, and never rereads admitted bytes."""
    with closing(LocalSqliteCoreLedger(tmp_path / "ledger.sqlite")) as ledger:
        blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
        publisher = CorePublisher(ledger, blobs)
        with publisher.session() as session:
            good, unused, old_input = (session.retain_bytes([value]) for value in (b"selected", b"unused", b"old input"))
            original = (
                definition(), entity("good", good), entity("unused", unused), entity("old-input", old_input),
                core.Request(format_version=1, request_id="request", definition_id="definition", inputs=(core.WholeInput(label="input", entity_id="old-input"),), dependencies=()),
                core.Execution(format_version=1, execution_id="execution", request_id="request"),
                outcome(outputs=(core.ResultBinding(label="selected", entity_id="good", role="derived", production="new"),
                                 core.ResultBinding(label="unused", entity_id="unused", role="derived", production="new")),
                        generations=(core.EntityEvent(event_id="g1", entity_id="good"), core.EntityEvent(event_id="g2", entity_id="unused"))),
            )
            session.publish(MetadataBatch("original", records=original, retained=(("result", "result"),)))
        policy = core.RetentionPolicy(format_version=1, policy_id="policy", description={"remove": "unselected test data"})
        ledger.commit(MetadataBatch("policy", records=(policy,)))
        with ledger.content_guard(exclusive=True):
            ledger.begin_removal("remove-unused", "policy", [("entity", "unused"), ("entity", "old-input")])
            for reference in (unused, old_input):
                blobs.delete(next(blob for blob in session.ready if blob.digest == reference.digest))
            ledger.finish_removal("remove-unused")
        selection = core.Selection(format_version=1, selection_id="selection", target=core.Origin(parent_entity_id="new-context"), request_id="new-request",
                                   selected_result_id="result", output_labels=("selected",))
        request = core.Request(format_version=1, request_id="new-request", definition_id="definition", inputs=(core.WholeInput(label="input", entity_id="new-input"),), dependencies=())
        batch = MetadataBatch("reuse", records=(selection, request, entity("new-input", core.InlineValue(value="new"))),
                              retained=(("selection", "selection"),), expected_versions=((("result", "result"), 0),))
        def unexpected(*args, **kwargs):
            raise AssertionError("reuse scanned admitted payload bytes")
        monkeypatch.setattr(blobs, "read", unexpected)
        monkeypatch.setattr(blobs, "verify", unexpected)
        with publisher.session() as session:
            assert session.publish(batch)
            assert not session.publish(batch)
        assert snapshot(ledger, ("selection", "selection")).retained
        assert snapshot(ledger, ("result", "result")).value == original[-1]
        assert not snapshot(ledger, ("entity", "unused")).available
        with publisher.session() as session, pytest.raises(StaleBaseError):
            session.publish(replace(batch, unit_id="stale", expected_versions=((("result", "result"), 1),)))
        evidence = core.DependencyEvidence(format_version=1, evidence_id="omission", result_id="result",
                                           status="omission", description={"dependency": "new evidence"})
        ledger.commit(MetadataBatch("evidence", records=(evidence,)))
        # Historical success reconciles; a fresh association must recheck the
        # updated evidence instead of carrying an earlier decision forward.
        with publisher.session() as session:
            assert not session.publish(batch)
            with pytest.raises(StaleBaseError):
                session.publish(replace(batch, unit_id="stale-after-evidence"))
