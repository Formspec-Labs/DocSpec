"""Adequacy, retained historical corrections, and shared binding evaluation."""

from contextlib import ExitStack

import msgspec
import pytest

from docspec.application.core_dependencies import CoreDependencies, corresponds
from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from docspec.ports.core_ledger import MetadataBatch
from tests.test_core_selections import fields, setup


def definition(*, uncertain=False):
    """Build a transformation definition, optionally declaring one uncertain `source` resource."""
    resources = (core.Resource(label="source", description={"version": "unknown"}, certainty="uncertain"),) if uncertain else ()
    return core.OperationDefinition(format_version=1, definition_id="definition", implementation_id="dependency-test",
        implementation_version="1", operation_kind="transformation", configuration={}, resources=resources)


def request(identity="q", *, parent="old", dependencies=None):
    """Build a request binding one whole input to `parent` plus a `/url` dependency by default."""
    return core.Request(format_version=1, request_id=identity, definition_id="definition",
        inputs=(core.WholeInput(label="input", entity_id=parent),), dependencies=tuple(dependencies) if dependencies is not None else (
            core.Dependency(label="url", binding_label="input", selection=fields("/url")),))


def retain(session, identity, value):
    """Publish an artifact entity and retain it under its own identity."""
    entity = core.Entity(format_version=1, entity_id=identity, entity_type="artifact", value=core.InlineValue(value=value))
    session.publish(MetadataBatch("retain:" + identity, records=(entity,), retained=(("entity", identity),)))


def original(session, *, result_id="result", uncertain=False, requested=None):
    """Publish one operation/request/execution/result set and return the operation and request."""
    requested = request() if requested is None else requested
    operation = definition(uncertain=uncertain)
    execution = core.Execution(format_version=1, execution_id=result_id + ":execution", request_id=requested.request_id)
    result = core.Result(format_version=1, result_id=result_id, execution_id=execution.execution_id,
                         outcome=core.Outcome(status="success", value="empty"))
    session.publish(MetadataBatch("original:" + result_id, records=(operation, requested, execution, result), retained=(("result", result_id),)))
    return operation, requested


def omission(identity="omission", *, scope="dependency", label="title", result_id="result"):
    """Build a dependency-omission evidence record for `result_id`."""
    description = core.DependencyOmission(scope=scope, label=label, reason="discovered material input")
    return core.DependencyEvidence(format_version=1, evidence_id=identity, result_id=result_id, status="omission",
                                   description=record_value(description, core.DependencyOmission))


def supplement(session, observation, *, identity="supplement", supersedes="omission", receipt="receipt", receipt_value=None):
    """Retain a receipt and build the supplement evidence that corrects `supersedes` with the observation."""
    payload = record_value(observation, core.HistoricalDependencyObservation)
    retain(session, receipt, payload if receipt_value is None else receipt_value)
    description = core.DependencySupplement(reason="retained original observation", observation=observation, supporting_entities=(receipt,))
    return core.DependencyEvidence(format_version=1, evidence_id=identity, result_id=observation.result_id, status="supplement",
        description=record_value(description, core.DependencySupplement), supersedes_evidence_id=supersedes)


def candidates(ledger, digest):
    """List result ids the ledger indexes under one correspondence digest, preserving order."""
    return [item.result_id for batch in ledger.find_candidates([("lookup", digest)]) for item in batch]


def test_metadata_only_change_and_material_change_use_the_same_selection_owner(tmp_path):
    """A metadata-only change keeps the original digest and adequacy; a bound-value change issues a new digest."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            for identity, value in [("old", {"url": "u", "title": "old"}), ("title", {"url": "u", "title": "new"}),
                                    ("url", {"url": "changed", "title": "old"})]:
                retain(session, identity, value)
            operation, requested = original(session)
            baseline = service.index_result(session, "result")
            assert baseline.adequate and candidates(ledger, baseline.digest) == ["result"]
            assert service.assess(session, request("changed-title", parent="title"), operation).digest == baseline.digest
            assert service.assess(session, request("changed-url", parent="url"), operation).digest != baseline.digest
            assert list(ledger.affected_results([("entity", "old")])) == [("result",)]
            assert list(ledger.affected_results([("operation_definition", "definition")])) == [("result",)]
            assert service.assess(session, requested, operation, result_id="result").effective_request == requested


def test_corrected_history_reindexes_without_changing_original_records(tmp_path):
    """A supplement yields a new adequacy digest while the stored request and original records stay byte-identical."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u", "title": "original"})
            operation, requested = original(session)
            before = service.index_result(session, "result")
            blocked = service.record_evidence(session, omission())
            assert not blocked.adequate and blocked.digest is None and blocked.unresolved_evidence_ids == ("omission",)
            observation = core.HistoricalDependencyObservation(result_id="result",
                dependency=core.Dependency(label="title", binding_label="input", selection=fields("/title")))
            record = supplement(session, observation)
            corrected = service.record_evidence(session, record)
            assert corrected.adequate and corrected.digest != before.digest
            assert candidates(ledger, corrected.digest) == candidates(ledger, before.digest) == ["result"]
            assert service.record_evidence(session, record).digest == corrected.digest
            assert len(corrected.effective_request.dependencies) == 2
            stored_request = next(ledger.read_records([("request", requested.request_id)]))[0]
            assert stored_request.value == requested and len(stored_request.value.dependencies) == 1
            assert service.assess(session, corrected.effective_request, operation).digest == corrected.digest
            assert dict(corrected.expected_versions)[("result", "result")] == 2
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            assert CoreDependencies().assess_result(session, "result").digest == corrected.digest


def test_unknown_resource_needs_original_observation_not_current_value(tmp_path):
    """An uncertain resource needs a retained historical observation; a current-value receipt cannot clear it."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            operation, requested = original(session, uncertain=True)
            unknown = service.index_result(session, "result")
            assert not unknown.adequate and unknown.uncertain_resources == ("source",) and unknown.digest is None
            service.record_evidence(session, omission(scope="resource", label="source"))
            resource = core.Resource(label="source", description={"version": "v1"}, certainty="established")
            observation = core.HistoricalDependencyObservation(result_id="result", resource=resource)
            current_only = supplement(session, observation, identity="current-only", receipt="current", receipt_value={"version": "v1"})
            with pytest.raises(IntegrityError):
                service.record_evidence(session, current_only)
            assert next(ledger.read_records([("dependency_evidence", "current-only")]))[0] is None
            corrected = service.record_evidence(session, supplement(session, observation))
            established = msgspec.structs.replace(operation, resources=(resource,))
            assert service.assess(session, requested, established).digest == corrected.digest
            assert service.assess(session, requested, operation).digest is None


@pytest.mark.parametrize("bad", ["unrelated", "cross-result", "self-reference", "uncertain", "conflicting-resource"])
def test_invalid_supplements_never_clear_omissions(tmp_path, bad):
    """Unrelated, cross-result, self-referential, uncertain or conflicting supplements all refuse and leave the omission."""
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            original(session)
            service.record_evidence(session, omission(scope="resource", label="source"))
            resource = core.Resource(label="wrong" if bad == "conflicting-resource" else "source", description={"v": 1},
                                     certainty="uncertain" if bad == "uncertain" else "established")
            observation = core.HistoricalDependencyObservation(result_id="other" if bad == "cross-result" else "result", resource=resource)
            record = supplement(session, observation, supersedes="absent" if bad == "unrelated" else "supplement" if bad == "self-reference" else "omission")
            if bad == "cross-result":
                record = msgspec.structs.replace(record, result_id="result")
            with pytest.raises(IntegrityError):
                service.record_evidence(session, record)
            assert not service.assess_result(session, "result").adequate


def test_one_correction_does_not_clear_an_unrelated_omission(tmp_path):
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u", "title": "t"})
            original(session)
            service.record_evidence(session, omission())
            service.record_evidence(session, omission("source-omission", scope="resource", label="source"))
            observation = core.HistoricalDependencyObservation(result_id="result",
                dependency=core.Dependency(label="title", binding_label="input", selection=fields("/title")))
            assessment = service.record_evidence(session, supplement(session, observation))
            assert not assessment.adequate and assessment.unresolved_evidence_ids == ("source-omission",)


def test_omission_is_recordable_when_original_input_is_no_longer_available(tmp_path):
    """A removed input makes the assessment unavailable and digestless while the omission is still recordable."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            original(session)
        policy = core.RetentionPolicy(format_version=1, policy_id="remove", description={"reason": "test"})
        ledger.commit(MetadataBatch("policy", records=(policy,)))
        ledger.begin_removal("remove-input", "remove", [("entity", "old")])
        with publisher.session() as session:
            before = CoreDependencies().assess_result(session, "result")
            assert before.adequate and before.unavailable_inputs == ("input",) and before.digest is None
            assert not CoreDependencies().record_evidence(session, omission()).adequate


def test_evidence_publication_refuses_a_concurrent_version_change(tmp_path, monkeypatch):
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            original(session)
            commit = ledger.commit
            def race(batch):
                if batch.unit_id == "dependency-evidence:omission":
                    concurrent = omission("concurrent", scope="resource", label="source")
                    commit(MetadataBatch("concurrent", records=(concurrent,), retained=(("dependency_evidence", "concurrent"),)))
                return commit(batch)
            monkeypatch.setattr(ledger, "commit", race)
            with pytest.raises(StaleBaseError):
                CoreDependencies().record_evidence(session, omission())
            assert next(ledger.read_records([("dependency_evidence", "omission")]))[0] is None


@pytest.mark.parametrize("replace_original", [False, True])
def test_added_historical_binding_requires_receipt_and_preserves_original_inputs(tmp_path, replace_original):
    """A historical input needs a retained receipt and may not replace an original request input."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            retain(session, "historical", "original omitted input")
            _, requested = original(session)
            service.record_evidence(session, omission())
            label = "input" if replace_original else "historical"
            observation = core.HistoricalDependencyObservation(result_id="result",
                dependency=core.Dependency(label="title", binding_label=label, selection=core.Whole()),
                historical_input=core.WholeInput(label=label, entity_id="historical"))
            record = supplement(session, observation)
            if replace_original:
                with pytest.raises(IntegrityError, match="replace an original"):
                    service.record_evidence(session, record)
            else:
                corrected = service.record_evidence(session, record)
                assert corrected.adequate and corrected.digest is not None
                assert len(corrected.effective_request.inputs) == 2
                assert next(ledger.read_records([("request", requested.request_id)]))[0].value == requested
                links = [link for batch in ledger.read_links([("dependency_evidence", "supplement")]) for link in batch]
                assert any(link.relation == "describes" and link.label == "historical_input" and link.target == ("entity", "historical") for link in links)


def test_unrecognized_evidence_refuses_and_closed_sessions_refuse(tmp_path):
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            original(session)
            malformed = msgspec.structs.replace(omission(), description={"unknown": "not an adequacy assertion"})
            ledger.commit(MetadataBatch("malformed", records=(malformed,), retained=(("dependency_evidence", "omission"),)))
            with pytest.raises(IntegrityError):
                CoreDependencies().assess_result(session, "result")
        with pytest.raises(StateTransitionError, match="closed"):
            CoreDependencies().assess_result(session, "result")


def remove_input(ledger, publisher, *, content=None):
    policy = core.RetentionPolicy(format_version=1, policy_id="input-retirement", description={"reason": "test retained comparison evidence"})
    ledger.commit(MetadataBatch("input-retirement-policy", records=(policy,)))
    with ledger.content_guard(exclusive=True):
        ledger.begin_removal("remove-old-input", policy.policy_id, [("entity", "old")])
        if content is not None:
            publisher.blobs.delete(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
        ledger.finish_removal("remove-old-input")


def comparison_artifacts(ledger):
    keys = [link.target for batch in ledger.read_links([("result", "result")]) for link in batch if link.relation == "comparison"]
    return [row.value for batch in ledger.read_records(keys) for row in batch]


def test_evaluated_comparison_survives_input_byte_removal_and_reopen(tmp_path, monkeypatch):
    """The indexed comparison snapshot answers after the input's bytes are removed and the store reopened."""
    with ExitStack() as stack:
        _, ledger, _, selections, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            content = session.retain_value({"url": "u", "title": "original"})
            entity = core.Entity(format_version=1, entity_id="old", entity_type="artifact", value=content)
            session.publish(MetadataBatch("input", records=(entity,), retained=(("entity", "old"),)))
            original(session)
            before = service.index_result(session, "result")
            snapshots = comparison_artifacts(ledger)
            assert len(snapshots) == 1
            payload = snapshots[0].value.value
            assert payload["evidence_version"] == 0 and payload["evidence_ids"] == []
            assert payload["digest"] == before.digest and payload["result_id"] == "result"
            monkeypatch.setattr(selections, "binding_evidence", lambda *args: pytest.fail("indexed inputs must not be evaluated again"))
            assert service.index_result(session, "result").digest == before.digest
        remove_input(ledger, publisher, content=content)
    with ExitStack() as stack:
        _, _, _, selections, publisher = setup(stack, tmp_path)
        monkeypatch.setattr(selections, "binding_evidence", lambda *args: pytest.fail("removed input must not be read"))
        with publisher.session() as session:
            after = CoreDependencies().assess_result(session, "result")
            assert corresponds(before, after)
            assert not after.unavailable_inputs and ("entity", "old") not in dict(after.expected_versions)
            assert after.support_keys == (("entity", snapshots[0].entity_id),)


def test_new_omission_blocks_old_snapshot_and_resource_correction_reuses_unchanged_values(tmp_path, monkeypatch):
    """A new omission invalidates the old snapshot; a resource correction reindexes without re-evaluating unchanged dependencies."""
    with ExitStack() as stack:
        _, ledger, _, selections, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            original(session)
            before = service.index_result(session, "result")
        remove_input(ledger, publisher)
        with publisher.session() as session:
            blocked = service.record_evidence(session, omission(scope="resource", label="source"))
            assert not blocked.adequate and not corresponds(before, blocked)
            assert len(comparison_artifacts(ledger)) == 1
            resource = core.Resource(label="source", description={"version": "original-v1"}, certainty="established")
            record = supplement(session, core.HistoricalDependencyObservation(result_id="result", resource=resource))
            monkeypatch.setattr(selections, "binding_evidence", lambda *args: pytest.fail("unchanged dependency must use retained evidence"))
            corrected = service.record_evidence(session, record)
            assert corrected.adequate and corrected.digest is not None and corrected.digest != before.digest
            snapshots = comparison_artifacts(ledger)
            current = next(artifact for artifact in snapshots if artifact.value.value["evidence_version"] == 2)
            assert current.value.value["evidence_ids"] == ["omission", "supplement"]
            assert ("entity", current.entity_id) in corrected.support_keys
            assert ("entity", "receipt") in corrected.support_keys
            assert ("dependency_evidence", "omission") in corrected.support_keys
            assert ("dependency_evidence", "supplement") in corrected.support_keys
            assert ("entity", "old") not in corrected.support_keys
            assert candidates(ledger, corrected.digest) == ["result"]


def test_new_selector_cannot_borrow_snapshot_values_from_another_selector(tmp_path):
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u", "title": "original"})
            original(session)
            before = service.index_result(session, "result")
        remove_input(ledger, publisher)
        with publisher.session() as session:
            service.record_evidence(session, omission(label="url"))
            correction = core.HistoricalDependencyObservation(result_id="result",
                dependency=core.Dependency(label="url", binding_label="input", selection=fields("/title")))
            assessment = service.record_evidence(session, supplement(session, correction))
            assert assessment.adequate and assessment.digest is None and assessment.unavailable_inputs == ("input",)
            assert not corresponds(before, assessment) and len(comparison_artifacts(ledger)) == 1
            retain(session, "current", {"url": "u", "title": "new value is not original history"})
            current = service.assess(session, request("current", parent="current", dependencies=assessment.effective_request.dependencies), definition())
            assert not corresponds(current, assessment)


@pytest.mark.parametrize("after_commit", [False, True])
def test_snapshot_and_candidate_publication_retry_is_atomic(tmp_path, monkeypatch, after_commit):
    """An interrupted snapshot publication leaves snapshot and candidate in the same state, and a retry converges."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            original(session)
            before = service.assess_result(session, "result")
            commit = ledger.commit
            def interrupted(batch):
                if batch.unit_id.startswith("dependencies:"):
                    if after_commit:
                        commit(batch)
                    raise OSError("interrupted comparison publication")
                return commit(batch)
            monkeypatch.setattr(ledger, "commit", interrupted)
            with pytest.raises(OSError):
                service.index_result(session, "result")
            assert len(comparison_artifacts(ledger)) == int(after_commit)
            assert candidates(ledger, before.digest) == (["result"] if after_commit else [])
            monkeypatch.setattr(ledger, "commit", commit)
            assert service.index_result(session, "result").digest == before.digest
            assert len(comparison_artifacts(ledger)) == 1


def test_snapshot_version_follows_correction_in_the_same_transaction(tmp_path, monkeypatch):
    """The corrected snapshot and its version bump share one transaction, so an interruption leaves neither."""
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u", "title": "t"})
            original(session)
            service.index_result(session, "result")
            service.record_evidence(session, omission())
            observation = core.HistoricalDependencyObservation(result_id="result",
                dependency=core.Dependency(label="title", binding_label="input", selection=fields("/title")))
            correction = supplement(session, observation)
            commit = ledger.commit
            def interrupted(batch):
                if batch.unit_id == "dependency-evidence:supplement":
                    assert len(batch.records) == 2
                    comparison = next(record for record in batch.records if isinstance(record, core.Entity))
                    assert comparison.value.value["evidence_version"] == 2
                    assert batch.candidates == ((comparison.value.value["digest"], "result"),)
                    assert dict(batch.expected_versions)[("result", "result")] == 1
                    raise OSError("before correction commit")
                return commit(batch)
            monkeypatch.setattr(ledger, "commit", interrupted)
            with pytest.raises(OSError):
                service.record_evidence(session, correction)
            assert len(comparison_artifacts(ledger)) == 1
            assert next(ledger.read_records([("dependency_evidence", "supplement")]))[0] is None
            monkeypatch.setattr(ledger, "commit", commit)
            assert service.record_evidence(session, correction).evidence_version == 2


def test_pending_data_can_supply_current_inputs_but_cannot_supply_historical_receipts(tmp_path):
    """Pending records may satisfy current inputs, but only retained records can support a historical receipt."""
    from docspec.application.core_publication import _PublicationReadView

    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        service = CoreDependencies()
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
            operation, _ = original(session)
            pending = core.Entity(format_version=1, entity_id="pending", entity_type="artifact", value=core.InlineValue(value={"url": "u"}))
            view = _PublicationReadView(session, {("entity", "pending"): pending}, ())
            assessment = service.assess(view, request("pending-request", parent="pending"), operation)
            assert assessment.adequate and assessment.digest is not None
            assert ("entity", "pending") not in dict(assessment.expected_versions)
            assert next(ledger.read_records([("entity", "pending")]))[0] is None
            service.record_evidence(session, omission(scope="resource", label="source"))
            observation = core.HistoricalDependencyObservation(result_id="result",
                resource=core.Resource(label="source", description={"v": 1}, certainty="established"))
            receipt = msgspec.structs.replace(pending, entity_id="receipt", value=core.InlineValue(value=record_value(observation, core.HistoricalDependencyObservation)))
            description = core.DependencySupplement(reason="assertion", observation=observation, supporting_entities=("receipt",))
            correction = core.DependencyEvidence(format_version=1, evidence_id="pending-supplement", result_id="result", status="supplement",
                description=record_value(description, core.DependencySupplement), supersedes_evidence_id="omission")
            view = _PublicationReadView(session, {("entity", "receipt"): receipt}, ())
            with pytest.raises(IntegrityError, match="retained"):
                service.record_evidence(view, correction)
