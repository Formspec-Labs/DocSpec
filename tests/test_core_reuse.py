"""Reuse preserves exact choices, adequacy, and the common attempt lifecycle."""

from contextlib import ExitStack
from msgspec.structs import replace

import pytest

from docspec.application.core_dependencies import CoreDependencies
from docspec.application.core_execution import CoreOperations, SuspendedOperation
from docspec.application.core_reuse import CoreReuse, ReuseRequest
from docspec.domain import core
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch
from tests.test_core_dependencies import definition, omission, request, retain, supplement
from tests.test_core_selections import setup


def resolve(operations, calls, *, identity, parent="old", fresh=False, policy=lambda result: True, operation=None):
    """Resolve one request with a recording producer, the identity-derived selection id and a parent origin."""
    def producer(context):
        calls.append(context.execution.execution_id)
        value = context.read_value(parent)
        context.generate(core.InlineValue(value={"url": value["url"], "run": len(calls)}), label="data")
    return operations.resolve(operation or definition(), request(identity, parent=parent), producer,
        selection_id=identity + ":selection", target=core.Origin(parent_entity_id=parent), reuse_policy=policy, fresh=fresh)


def test_metadata_only_reuse_and_explicit_fresh_alternatives_preserve_exact_choices(tmp_path):
    """Metadata-only changes reuse the result, explicit fresh work makes a new one, and a historical choice never reruns today's policy."""
    calls = []
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u", "title": "old"})
            retain(session, "retitled", {"url": "u", "title": "new"})
            retain(session, "changed", {"url": "v"})
        operations = CoreOperations(publisher)
        first = resolve(operations, calls, identity="first")
        reused = resolve(operations, calls, identity="retitled", parent="retitled")
        assert reused.result == first.result and len(calls) == 1
        assert reused.selection.request_id == "retitled"
        fresh = resolve(operations, calls, identity="fresh", fresh=True)
        assert fresh.result.result_id != first.result.result_id and len(calls) == 2
        chosen = resolve(operations, calls, identity="chosen", policy=lambda result: result.result_id == fresh.result.result_id)
        assert chosen.result == fresh.result and len(calls) == 2
        material = resolve(operations, calls, identity="material", parent="changed")
        assert material.result.result_id not in {first.result.result_id, fresh.result.result_id} and len(calls) == 3
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='execution'").fetchone() == (3,)
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        def never(result):
            raise AssertionError("historical choice reran today's policy")
        restored = resolve(CoreOperations(publisher), calls, identity="chosen", policy=never)
        assert restored == chosen and len(calls) == 3


def test_omission_blocks_reuse_and_later_resource_evidence_uses_effective_definition(tmp_path):
    """An omission blocks reuse, and a corrected resource reuses the earlier result under its effective definition."""
    calls = []
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u", "title": "old"})
        operations = CoreOperations(publisher)
        first = resolve(operations, calls, identity="first")
        with publisher.session() as session:
            CoreDependencies().record_evidence(session, omission(result_id=first.result.result_id))
        assert resolve(operations, calls, identity="after-omission").result != first.result
        unknown_definition = replace(definition(uncertain=True), definition_id="unknown")
        unknown_request = replace(request("unknown-request"), definition_id="unknown")
        unknown = operations.resolve(unknown_definition, unknown_request, lambda context: None,
            selection_id="unknown-selection", target=core.Origin(parent_entity_id="old"), reuse_policy=lambda result: True)
        resource = core.Resource(label="source", description={"version": "v1"}, certainty="established")
        with publisher.session() as session:
            service = CoreDependencies()
            service.record_evidence(session, omission("resource-missing", scope="resource", label="source", result_id=unknown.result.result_id))
            observation = core.HistoricalDependencyObservation(result_id=unknown.result.result_id, resource=resource)
            service.record_evidence(session, supplement(session, observation, supersedes="resource-missing"))
        established = replace(unknown_definition, definition_id="established", resources=(resource,))
        result = operations.resolve(established, replace(unknown_request, request_id="established-request", definition_id="established"),
            lambda context: pytest.fail("corrected resource was not reused"), selection_id="established-selection",
            target=core.Origin(parent_entity_id="old"), reuse_policy=lambda result: True)
        assert result.result == unknown.result


def test_batch_candidates_skip_unavailable_outputs_and_keep_multiple_results(tmp_path):
    """Batch reuse skips a result whose output bytes were removed and returns one choice per request."""
    calls = []
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        operations = CoreOperations(publisher)
        first = resolve(operations, calls, identity="first")
        other = resolve(operations, calls, identity="other", fresh=True)
        first, other = sorted((first, other), key=lambda item: item.result.result_id)
        policy = core.RetentionPolicy(format_version=1, policy_id="remove", description={"reason": "test"})
        ledger.commit(MetadataBatch("policy", records=(policy,)))
        ledger.begin_removal("remove-output", "remove", [("entity", first.result.outcome.outputs[0].entity_id)])
        with publisher.session() as session:
            chosen = CoreReuse().choose_many(session, (
                ReuseRequest(definition(), request(f"batch-{index}"), f"batch-{index}", core.Origin(parent_entity_id="old"), lambda result: True)
                for index in range(3)))
        assert all(item.result == other.result for item in chosen)
        assert len(calls) == 2


def test_publisher_refuses_forged_reuse_but_evaluates_staged_current_inputs(tmp_path):
    """A forged selection whose input does not correspond refuses, while a matching staged input is admitted."""
    calls = []
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        first = resolve(CoreOperations(publisher), calls, identity="first")
        for url, allowed in [("v", False), ("u", True)]:
            identity = "new-" + url
            data = core.Entity(format_version=1, entity_id=identity, entity_type="artifact", value=core.InlineValue(value={"url": url}))
            wanted = request(identity, parent=identity)
            selection = core.Selection(format_version=1, selection_id=identity, target=core.Origin(parent_entity_id=identity),
                request_id=identity, selected_result_id=first.result.result_id, output_labels=("data",))
            batch = MetadataBatch(identity, records=(data, wanted, selection), retained=(("selection", identity),),
                expected_versions=((("result", first.result.result_id), 0),))
            with publisher.session() as session:
                if allowed:
                    assert session.publish(batch)
                else:
                    with pytest.raises(IntegrityError, match="does not correspond"):
                        session.publish(batch)
                    assert next(ledger.read_records([("entity", identity)]))[0] is None


def test_resolved_attempt_continues_and_publishes_its_original_selection(tmp_path):
    """A suspended resolved attempt continues and publishes its original selection; a later resolve returns that same result."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        def producer(context):
            context.generate(core.InlineValue(value="prefix"), label="prefix")
            context.suspend({"position": 1})
        pending = operations.resolve(definition(), request("continued"), producer, selection_id="continued-selection",
            target=core.Origin(parent_entity_id="old"), reuse_policy=lambda result: True)
        assert isinstance(pending, SuspendedOperation)
        result = operations.resume(pending.execution_id, lambda context, state: None, definition=definition(), verify=lambda state: state == {"position": 1})
        selection = next(ledger.read_records([("selection", "continued-selection")]))[0]
        assert selection.retained and selection.value.selected_result_id == result.result_id
        recovered = operations.resolve(definition(), request("continued"), lambda context: pytest.fail("reran producer"),
            selection_id="continued-selection", target=core.Origin(parent_entity_id="old"), reuse_policy=lambda result: False)
        assert recovered.result == result


def test_new_input_records_use_the_existing_import_and_execution_path(tmp_path):
    """Input records passed to resolve are imported through the existing path and yield an empty success."""
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        data = core.Entity(format_version=1, entity_id="old", entity_type="artifact", value=core.InlineValue(value={"url": "u"}))
        resolution = CoreOperations(publisher).resolve(definition(), request(), lambda context: context.read_value("old") and None,
            selection_id="selected", target=core.Origin(parent_entity_id="old"), reuse_policy=lambda result: True, input_records=(data,))
        assert resolution.result.outcome.value == "empty"


def test_reuse_after_old_input_deletion_retains_comparison_evidence_and_new_inputs(tmp_path):
    """After the old input's bytes are deleted, reuse answers from retained comparison evidence and keeps the new input."""
    calls = []
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            content = session.retain_value({"url": "u", "title": "historical"})
            data = core.Entity(format_version=1, entity_id="old", entity_type="artifact", value=content)
            session.publish(MetadataBatch("old", records=(data,), retained=(("entity", "old"),)))
            retain(session, "new", {"url": "u", "title": "current"})
        first = resolve(CoreOperations(publisher), calls, identity="first")
        policy = core.RetentionPolicy(format_version=1, policy_id="policy", description={"scope": "old input"})
        ledger.commit(MetadataBatch("policy", records=(policy,)))
        with ledger.content_guard(exclusive=True):
            ledger.begin_removal("delete-old", "policy", [("entity", "old")])
            from docspec.domain.references import BlobRef
            publisher.blobs.delete(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
            ledger.finish_removal("delete-old")
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        selected = resolve(CoreOperations(publisher), calls, identity="reuse-after-loss", parent="new")
        assert selected.result == first.result and len(calls) == 1
        links = [link for batch in ledger.read_links([("selection", selected.selection.selection_id)]) for link in batch]
        supports = [link.target for link in links if link.label == "comparison_evidence"]
        assert supports and ("entity", "old") not in supports
        assert next(ledger.read_records([("entity", "new")]))[0].retained
        restored = resolve(CoreOperations(publisher), calls, identity="reuse-after-loss", parent="new")
        assert restored == selected


def test_policy_cannot_waive_a_new_omission_between_lookup_and_commit(tmp_path):
    """An omission recorded between lookup and commit defeats the reuse policy, so a fresh attempt runs."""
    calls, policies = [], []
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        operations = CoreOperations(publisher)
        first = resolve(operations, calls, identity="first")
        def stale_policy(result):
            policies.append(result.result_id)
            with publisher.session() as session:
                CoreDependencies().record_evidence(session, omission(result_id=result.result_id))
            return True
        selected = resolve(operations, calls, identity="after-race", policy=stale_policy)
        assert selected.result != first.result and len(calls) == 2
        assert policies == [first.result.result_id]
        assert next(ledger.read_records([("result", first.result.result_id)]))[0].value == first.result


def test_failed_policy_is_visible_and_does_not_trigger_execution(tmp_path):
    """A reuse policy that raises propagates without running the producer."""
    calls = []
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        operations = CoreOperations(publisher)
        resolve(operations, calls, identity="first")
        def failed(result):
            raise RuntimeError("policy unavailable")
        with pytest.raises(RuntimeError, match="policy unavailable"):
            resolve(operations, calls, identity="failed-policy", policy=failed)
        assert len(calls) == 1


def test_supplemented_historical_input_can_be_removed_after_comparison_is_retained(tmp_path):
    """Once the supplemented comparison is retained, the historical input can be removed and reuse still answers."""
    from docspec.application.core_maintenance import CoreMaintenance
    from tests.test_core_dependencies import original
    from tests.test_core_selections import fields
    from tests.test_core_maintenance import authorize
    with ExitStack() as stack:
        records, ledger, _, _, publisher = setup(stack, tmp_path)
        dependencies = CoreDependencies()
        with publisher.session() as session:
            content = session.retain_value({"url": "u", "large": "historical input" * 100})
            historical = core.Entity(format_version=1, entity_id="historical", entity_type="artifact", value=content)
            session.publish(MetadataBatch("historical", records=(historical,), retained=(("entity", "historical"),)))
            retain(session, "new", {"url": "u", "large": "current input"})
            empty = core.Request(format_version=1, request_id="empty", definition_id="definition", inputs=(), dependencies=())
            original(session, requested=empty)
            dependencies.record_evidence(session, omission(label="url"))
            observation = core.HistoricalDependencyObservation(result_id="result",
                dependency=core.Dependency(label="url", binding_label="input", selection=fields("/url")),
                historical_input=core.WholeInput(label="input", entity_id="historical"))
            assessment = dependencies.record_evidence(session, supplement(session, observation))
            assert assessment.digest is not None
        authorize(publisher, [("entity", "historical")])
        assert CoreMaintenance(publisher, records).remove_under_policy("remove-historical", "policy", [("entity", "historical")]) == {"deleted": 1}
        calls = []
        selected = CoreOperations(publisher).resolve(definition(), request("now", parent="new"),
            lambda context: calls.append(context.execution.execution_id), selection_id="selected-now",
            target=core.Origin(parent_entity_id="new"), reuse_policy=lambda result: True)
        assert selected.result.result_id == "result" and calls == []
        assert next(ledger.read_records([("entity", "historical")]))[0].available is False
