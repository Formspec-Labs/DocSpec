"""Declared dependency evidence and adequacy, shared by indexing and reuse.

The supplied request declares material inputs. This owner evaluates that
declaration; it does not infer the dependencies of arbitrary Python code.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import msgspec

from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.core_encoding import correspondence_bytes
from docspec.domain.identity import canonical_value_bytes, sha256_digest, snapshot_json_value
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch, MetadataLink, RecordKey
from docspec.ports.record_storage import BATCH_ROWS


def _typed(value, record_type):
    return msgspec.convert(snapshot_json_value(record_value(value, record_type)), type=record_type, strict=True)


def binding_key(binding):
    if isinstance(binding, core.WholeInput):
        return "entity", binding.entity_id
    if isinstance(binding, core.StateInput):
        return "state", binding.state_id
    return "selected_value", binding.selected_value_id


@dataclass(frozen=True, slots=True)
class DependencyAssessment:
    effective_definition: core.OperationDefinition
    effective_request: core.Request
    dependencies: Mapping[str, tuple[core.Selector, core.ComparisonEvidence]]
    digest: str | None
    adequate: bool
    unresolved_evidence_ids: tuple[str, ...]
    uncertain_resources: tuple[str, ...]
    unavailable_inputs: tuple[str, ...]
    expected_versions: tuple[tuple[RecordKey, int], ...]
    links: tuple[MetadataLink, ...]
    evidence_ids: tuple[str, ...] = ()
    evidence_version: int = 0
    support_keys: tuple[RecordKey, ...] = ()


def corresponds(requested: DependencyAssessment, retained: DependencyAssessment) -> bool:
    """Adequacy and exact evaluated meaning precede any reuse policy."""
    return bool(requested.adequate and retained.adequate and requested.digest is not None and retained.digest is not None
                and requested.digest == retained.digest
                and correspondence_bytes(requested.effective_definition, requested.dependencies)
                == correspondence_bytes(retained.effective_definition, retained.dependencies))


class _EvidenceRead:
    def __init__(self, session):
        self.session = session
        self.rows = {}
        self.guards = set()
        self.support = set()

    def load(self, keys, *, available=False, historical=False, guard=True):
        keys = bounded_items(keys, limit=BATCH_ROWS)
        missing = tuple(dict.fromkeys(key for key in keys if key not in self.rows))
        if missing:
            with owned_iterator(self.session.read_records(missing)) as batches:
                rows = [row for batch in batches for row in batch]
            self.rows.update(zip(missing, rows, strict=True))
        result = []
        for key in keys:
            row = self.rows[key]
            pending = key in self.session.pending_keys
            if row is None or (not row.retained and not pending) or (historical and pending) or (available and not row.available):
                raise IntegrityError(f"dependency evidence requires retained {'available ' if available else ''}{key[0]}: {key[1]}")
            result.append(row.value)
        if guard:
            self.guards.update(keys)
        return tuple(result)

    def one(self, key, *, available=False, historical=False, guard=True):
        return self.load([key], available=available, historical=historical, guard=guard)[0]

    def versions(self):
        return tuple(sorted((key, row.evidence_version) for key, row in self.rows.items()
                            if row is not None and key in self.guards and key not in self.session.pending_keys))


class CoreDependencies:
    """Use existing ledger transactions and selected-value evaluation."""

    @staticmethod
    def _original(reader, result_id):
        result = reader.one(("result", result_id), historical=True)
        if result.outcome.status != "success":
            raise IntegrityError("correspondence requires a successful retained result")
        execution = reader.one(("execution", result.execution_id), historical=True)
        request = reader.one(("request", execution.request_id), historical=True)
        definition = reader.one(("operation_definition", request.definition_id), historical=True)
        return request, definition

    @staticmethod
    def _evidence(reader, result_id):
        with owned_iterator(reader.session.ledger.read_dependencies([result_id])) as batches:
            keys = bounded_items((link.target for batch in batches for link in batch if link.relation == "evidence"), limit=BATCH_ROWS)
        values = reader.load(keys, historical=True)
        reader.support.update(keys)
        return values

    @staticmethod
    def _receipt(reader, supplement, result_id):
        observation = supplement.observation
        if observation.result_id != result_id:
            raise IntegrityError("a supplement must describe its original result")
        if not supplement.supporting_entities or len(set(supplement.supporting_entities)) != len(supplement.supporting_entities):
            raise IntegrityError("a supplement requires distinct retained historical observation receipts")
        expected = canonical_value_bytes(record_value(observation, core.HistoricalDependencyObservation))
        for receipt in reader.load((("entity", identity) for identity in supplement.supporting_entities), available=True, historical=True):
            value = receipt.value
            if isinstance(value, core.InlineValue):
                payload = value.value
            else:
                payload = reader.session.read_json(value, label="historical dependency observation")
            admitted = _typed(payload, core.HistoricalDependencyObservation)
            if canonical_value_bytes(record_value(admitted, core.HistoricalDependencyObservation)) != expected:
                raise IntegrityError("supporting receipt does not establish the stated historical dependency observation")
            reader.support.add(("entity", receipt.entity_id))

    def _fold(self, reader, request, definition, result_id, evidence, *, admitting=()):
        resources = {item.label: item for item in definition.resources}
        dependencies = {item.label: item for item in request.dependencies}
        inputs = {item.label: item for item in request.inputs}
        omissions, supplements = {}, []
        for record in evidence:
            if record.result_id != result_id:
                raise IntegrityError("dependency evidence identifies another result")
            if record.status == "omission":
                if record.supersedes_evidence_id is not None:
                    raise IntegrityError("an omission cannot silently supersede existing evidence")
                omissions[record.evidence_id] = _typed(record.description, core.DependencyOmission)
            else:
                supplements.append((record, _typed(record.description, core.DependencySupplement)))
        resolved, corrections = set(), {}
        for record, supplement in supplements:
            target = record.supersedes_evidence_id
            if target not in omissions:
                raise IntegrityError("a supplement must identify an omission on the same result")
            omission = omissions[target]
            observation = supplement.observation
            self._receipt(reader, supplement, result_id)
            resource, dependency = observation.resource, observation.dependency
            if omission.scope == "resource":
                if resource is None or dependency is not None or observation.historical_input is not None:
                    raise IntegrityError("a resource supplement requires exactly its corrected resource")
                if resource.label != omission.label or resource.certainty != "established":
                    raise IntegrityError("a resource supplement must establish the omitted resource")
                value = resource
            else:
                if dependency is None or resource is not None or dependency.label != omission.label:
                    raise IntegrityError("a dependency supplement requires exactly its corrected dependency")
                historical = observation.historical_input
                if historical is not None:
                    if historical.label != dependency.binding_label:
                        raise IntegrityError("historical input label differs from the corrected dependency")
                    if historical.label in inputs and inputs[historical.label] != historical:
                        raise IntegrityError("a supplement cannot replace an original input binding")
                    if record.evidence_id in admitting:
                        reader.one(binding_key(historical), available=True, historical=True)
                    inputs[historical.label] = historical
                if dependency.binding_label not in inputs:
                    raise IntegrityError("an omitted historical input requires an explicit retained binding")
                value = dependency
            key = omission.scope, omission.label
            encoded = canonical_value_bytes(record_value(value, type(value)))
            if key in corrections and corrections[key] != encoded:
                raise IntegrityError("historical dependency corrections conflict")
            corrections[key] = encoded
            (resources if resource is not None else dependencies)[omission.label] = value
            resolved.add(target)
        effective_definition = _typed(msgspec.structs.replace(definition, resources=tuple(resources.values())), core.OperationDefinition)
        effective_request = _typed(msgspec.structs.replace(request, inputs=tuple(inputs.values()), dependencies=tuple(dependencies.values())), core.Request)
        return effective_request, effective_definition, tuple(sorted(set(omissions) - resolved))

    @staticmethod
    def _meaning(dependency, binding):
        return canonical_value_bytes([record_value(dependency, core.Dependency), record_value(binding, core.InputBinding)])

    def _comparisons(self, reader, result_id, definition, evidence_ids, version, wanted):
        """Previously evaluated immutable inputs remain evidence after byte loss.

        The caller has already folded current omissions. This only supplies
        values for an exactly matching declared selector and input binding.
        """
        if not wanted:
            return {}
        with owned_iterator(reader.session.read_links([("result", result_id)])) as batches:
            links = bounded_items((link for batch in batches for link in batch if link.relation == "comparison"), limit=BATCH_ROWS)
        if any(not link.label.isdecimal() for link in links):
            raise IntegrityError("dependency comparison link requires an evidence version")
        comparisons = {}
        for link in sorted(links, key=lambda link: (int(link.label), link.target), reverse=True):
            artifact = reader.one(link.target, historical=True, guard=False)
            if not isinstance(artifact, core.Entity) or artifact.entity_type != "artifact" or not isinstance(artifact.value, core.InlineValue):
                raise IntegrityError("dependency comparison must be a retained inline artifact")
            key = "entity", artifact.entity_id
            if not reader.rows[key].available:
                continue
            snapshot = _typed(artifact.value.value, core.DependencyComparison)
            payload = canonical_value_bytes(record_value(snapshot, core.DependencyComparison))
            if (artifact.entity_id != "comparison:" + sha256_digest(payload) or snapshot.result_id != result_id
                    or str(snapshot.evidence_version) != link.label):
                raise IntegrityError("dependency comparison differs from its pinned identity or original result")
            if snapshot.evidence_version > version or not set(snapshot.evidence_ids) <= set(evidence_ids):
                continue
            if msgspec.structs.replace(snapshot.definition, resources=()) != msgspec.structs.replace(definition, resources=()):
                raise IntegrityError("dependency comparison identifies a different operation")
            if len(set(snapshot.evidence_ids)) != len(snapshot.evidence_ids):
                raise IntegrityError("dependency comparison repeats evidence identities")
            labels = [entry.dependency.label for entry in snapshot.dependencies]
            if len(set(labels)) != len(labels):
                raise IntegrityError("dependency comparison repeats dependency labels")
            fingerprint = {}
            for entry in snapshot.dependencies:
                record_value(entry.dependency.selection, core.Selector)
                record_value(entry.selector, core.Selector)
                if entry.binding.label != entry.dependency.binding_label:
                    raise IntegrityError("dependency comparison binding differs from its declaration")
                fingerprint[entry.dependency.label] = entry.selector, entry.evidence
                meaning = self._meaning(entry.dependency, entry.binding)
                value = entry.selector, entry.evidence
                if meaning in wanted:
                    comparisons.setdefault(meaning, (*value, key))
            if sha256_digest(correspondence_bytes(snapshot.definition, fingerprint)) != snapshot.digest:
                raise IntegrityError("dependency comparison fingerprint differs from its evaluated values")
            if wanted <= comparisons.keys():
                break
        return comparisons

    @staticmethod
    def _snapshot(assessment, result_id):
        """Prepare one metadata artifact and link for the caller's transaction."""
        if assessment.digest is None:
            return (), (), ()
        bindings = {binding.label: binding for binding in assessment.effective_request.inputs}
        entries = tuple(core.EvaluatedDependency(dependency=dependency, binding=bindings[dependency.binding_label],
                                                 selector=assessment.dependencies[dependency.label][0],
                                                 evidence=assessment.dependencies[dependency.label][1])
                        for dependency in assessment.effective_request.dependencies)
        snapshot = core.DependencyComparison(result_id=result_id, evidence_version=assessment.evidence_version,
            definition=assessment.effective_definition, dependencies=entries, evidence_ids=assessment.evidence_ids, digest=assessment.digest)
        payload = record_value(snapshot, core.DependencyComparison)
        identity = "comparison:" + sha256_digest(canonical_value_bytes(payload))
        artifact = core.Entity(format_version=1, entity_id=identity, entity_type="artifact", value=core.InlineValue(value=payload))
        key = "entity", identity
        link = MetadataLink(("result", result_id), "comparison", str(assessment.evidence_version), key)
        return (artifact,), (key,), (link,)

    def _assess(self, reader, request, definition, *, result_id=None, additions=()):
        request, definition = _typed(request, core.Request), _typed(definition, core.OperationDefinition)
        if request.definition_id != definition.definition_id:
            raise IntegrityError("request refers to another operation definition")
        if result_id is not None:
            original_request, original_definition = self._original(reader, result_id)
            if request != original_request or definition != original_definition:
                raise IntegrityError("result assessment must start from its original request and definition")
            evidence = {record.evidence_id: record for record in self._evidence(reader, result_id)}
            existing_ids = set(evidence)
            for addition in additions:
                addition = _typed(addition, core.DependencyEvidence)
                if addition.evidence_id in evidence and addition != evidence[addition.evidence_id]:
                    raise IntegrityError("dependency evidence identity is immutable")
                evidence[addition.evidence_id] = addition
            admitting = set(evidence) - existing_ids
            request, definition, unresolved = self._fold(reader, request, definition, result_id, evidence.values(), admitting=admitting)
            evidence_ids = tuple(sorted(evidence))
            version = reader.rows[("result", result_id)].evidence_version
        else:
            if additions:
                raise IntegrityError("historical evidence requires its original result")
            unresolved = ()
            evidence_ids, version, admitting = (), 0, ()
        uncertain = tuple(sorted(resource.label for resource in definition.resources if resource.certainty == "uncertain"))
        adequate = not unresolved and not uncertain
        bindings = {binding.label: binding for binding in request.inputs}
        links = (MetadataLink(("result", result_id), "dependency", "operation_definition",
                              ("operation_definition", definition.definition_id)), *(
            MetadataLink(("result", result_id), "dependency", dependency.label, binding_key(bindings[dependency.binding_label]))
            for dependency in request.dependencies)) if result_id is not None else ()
        comparison, unavailable = {}, []
        if adequate:
            wanted = {self._meaning(dependency, bindings[dependency.binding_label]) for dependency in request.dependencies}
            retained = self._comparisons(reader, result_id, definition, evidence_ids, version, wanted) if result_id is not None else {}
            needed = tuple(dependency for dependency in request.dependencies
                           if self._meaning(dependency, bindings[dependency.binding_label]) not in retained)
            reader.load(binding_key(bindings[dependency.binding_label]) for dependency in needed)
            for dependency in request.dependencies:
                binding = bindings[dependency.binding_label]
                meaning = self._meaning(dependency, binding)
                if meaning in retained:
                    selector, evidence, support_key = retained[meaning]
                    comparison[dependency.label] = selector, evidence
                    reader.guards.add(support_key)
                    reader.support.add(support_key)
                    continue
                if not reader.rows[binding_key(binding)].available:
                    unavailable.append(binding.label)
                    reader.guards.discard(binding_key(binding))
                    continue
                if reader.session.selections is None:
                    raise IntegrityError("dependency comparison requires the selected-value owner")
                comparison[dependency.label] = reader.session.selections.binding_evidence(
                    reader.session, binding, dependency.selection, guard=lambda keys: reader.load(keys, available=True))
        digest = sha256_digest(correspondence_bytes(definition, comparison)) if adequate and not unavailable else None
        return DependencyAssessment(definition, request, MappingProxyType(comparison), digest, adequate, unresolved, uncertain,
                                    tuple(sorted(set(unavailable))), reader.versions(), links, evidence_ids, version + len(admitting),
                                    tuple(sorted(reader.support)))

    def assess(self, session, request, definition, *, result_id=None):
        session._active()
        return self._assess(_EvidenceRead(session), request, definition, result_id=result_id)

    def assess_result(self, session, result_id):
        session._active()
        reader = _EvidenceRead(session)
        request, definition = self._original(reader, result_id)
        return self._assess(reader, request, definition, result_id=result_id)

    def index_result(self, session, result_id):
        assessment = self.assess_result(session, result_id)
        version = dict(assessment.expected_versions)[("result", result_id)]
        records, retained, links = self._snapshot(assessment, result_id)
        session.ledger.commit(MetadataBatch(f"dependencies:{result_id}:{version}:{assessment.digest}",
            records=records, retained=retained,
            candidates=((assessment.digest, result_id),) if assessment.digest is not None else (),
            links=(*assessment.links, *links), expected_versions=assessment.expected_versions))
        return assessment

    def record_evidence(self, session, evidence):
        session._active()
        evidence = _typed(evidence, core.DependencyEvidence)
        unit_id = "dependency-evidence:" + evidence.evidence_id
        if session.ledger.is_committed(unit_id):
            existing = _EvidenceRead(session).one(("dependency_evidence", evidence.evidence_id))
            if existing != evidence:
                raise IntegrityError("dependency evidence identity is immutable")
            return self.assess_result(session, evidence.result_id)
        reader = _EvidenceRead(session)
        request, definition = self._original(reader, evidence.result_id)
        assessment = self._assess(reader, request, definition, result_id=evidence.result_id, additions=(evidence,))
        records, retained, comparison_links = self._snapshot(assessment, evidence.result_id)
        links = list((*assessment.links, *comparison_links))
        if evidence.status == "supplement":
            supplement = _typed(evidence.description, core.DependencySupplement)
            links.extend(MetadataLink(("dependency_evidence", evidence.evidence_id), "requires", "historical_receipt", ("entity", identity))
                         for identity in supplement.supporting_entities)
            if supplement.observation.historical_input is not None:
                links.append(MetadataLink(("dependency_evidence", evidence.evidence_id), "describes", "historical_input",
                                          binding_key(supplement.observation.historical_input)))
        session.ledger.commit(MetadataBatch(unit_id, records=(evidence, *records), retained=(("dependency_evidence", evidence.evidence_id), *retained),
            candidates=((assessment.digest, evidence.result_id),) if assessment.digest is not None else (),
            links=tuple(links), expected_versions=assessment.expected_versions))
        return self.assess_result(session, evidence.result_id)
