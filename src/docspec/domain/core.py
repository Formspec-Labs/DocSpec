"""Version 1 Core records, shared by API admission and retained batch rows.

Entities, states and operation definitions have PROV entity identities while
executions have activity identities; roles belong to output bindings, and
physical representations, content digests, member keys and request IDs stay
separate. These are data declarations, not a second application lifecycle.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import msgspec


Identifier = Annotated[str, msgspec.Meta(min_length=1)]
Digest = Annotated[str, msgspec.Meta(pattern=r"^sha256:[0-9a-f]{64}$")]
Count = Annotated[int, msgspec.Meta(ge=0, le=2**53 - 1)]


class Fixed(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True, tag_field="kind"):
    """One closed record shape; cross-record checks belong to admission owners."""


class Record(Fixed, kw_only=True):
    format_version: Literal[1]


class InlineValue(Fixed, tag="inline"):
    """A value carried directly inside the record under the ``json-v1`` codec."""

    value: Any
    codec: Literal["json-v1"] = "json-v1"


class ContentRef(Fixed, tag="content"):
    """A value stored as content bytes, addressed by digest, size, and locator."""

    digest: Digest
    byte_size: Count
    locator: Identifier
    media_type: Identifier
    codec: Literal["json-v1", "bytes-v1"] = "bytes-v1"


ValueRef = InlineValue | ContentRef


class ComparisonEvidence(Fixed):
    """Derived comparison metadata; retained values establish its correctness."""

    codec: Literal["json-v1", "bytes-v1", "members-v1"]
    digest: Digest
    byte_size: Count
    entity_id: Identifier | None = None


class Entity(Record, tag="entity", kw_only=True):
    entity_id: Identifier
    entity_type: Literal["occurrence", "artifact"]
    value: ValueRef


class Membership(Fixed):
    member_key: str
    occurrence_id: Identifier


class State(Record, tag="state", kw_only=True):
    state_id: Identifier


class StateRepresentation(Record, tag="state_representation", kw_only=True):
    """A physical checkpoint may change representation_id while state_id stays."""

    representation_id: Identifier
    state_id: Identifier
    membership: tuple[Membership, ...] | ContentRef
    revision_id: Identifier | None = None


class Put(Fixed, tag="put"):
    sequence: Count
    member_key: str
    occurrence_id: Identifier


class Remove(Fixed, tag="remove"):
    sequence: Count
    member_key: str


class ValueEdit(Fixed):
    source_occurrence_id: Identifier
    result_occurrence_id: Identifier
    execution_id: Identifier
    # RFC 6902 ignores unknown operation members; they are not Fixed records.
    patch: tuple[dict[str, Any], ...]


class Revision(Record, tag="revision", kw_only=True):
    revision_id: Identifier
    base_state_id: Identifier
    result_state_id: Identifier
    edits: tuple[Put | Remove, ...] | ContentRef
    value_edits: tuple[ValueEdit, ...] = ()


class Whole(Fixed, tag="whole"):
    """Select the entity's whole value under value or identity comparison."""

    version: Literal[1] = 1
    comparison: Literal["value", "identity"] = "value"


class Field(Fixed):
    label: Identifier
    pointer: str


class JsonFields(Fixed, tag="json_fields"):
    """Select labeled fields addressed by JSON Pointer from the entity value."""

    selectors: tuple[Field, ...]
    version: Literal[1] = 1
    comparison: Literal["value", "identity"] = "value"


class StateMembers(Fixed, tag="state_members"):
    """Select state membership, optionally scoped, sorted, and with material keys."""

    member_selector: Whole | JsonFields
    version: Literal[1] = 1
    scope: tuple[str, ...] | None = None
    material_keys: bool = False
    sort_rule: Identifier | None = None


Selector = Whole | JsonFields | StateMembers


class Origin(Fixed):
    parent_entity_id: Identifier
    state_id: Identifier | None = None
    member_key: str | None = None


class MemberOrigin(Fixed):
    member_key: str
    occurrence_id: Identifier | None  # None records a missing named member.


class FromParent(Fixed, tag="from_parent"):
    """Recover with this selected-value record's definition and immutable origin."""


class SelectedValue(Record, tag="selected_value", kw_only=True):
    selected_value_id: Identifier
    definition: Selector
    origin: Origin
    value: ValueRef | FromParent
    member_origins: tuple[MemberOrigin, ...] | ContentRef = ()


class WholeInput(Fixed, tag="whole_value"):
    label: Identifier
    entity_id: Identifier


class StateInput(Fixed, tag="dataset_state"):
    label: Identifier
    state_id: Identifier


class SelectedInput(Fixed, tag="selected_value"):
    label: Identifier
    selected_value_id: Identifier


InputBinding = WholeInput | StateInput | SelectedInput


class Resource(Fixed):
    label: Identifier
    description: dict[str, Any]
    certainty: Literal["established", "uncertain"]


class OperationDefinition(Record, tag="operation_definition", kw_only=True):
    """A separately identified definition is the Plan of a PROV Association."""

    definition_id: Identifier
    implementation_id: Identifier
    implementation_version: Identifier
    operation_kind: Literal["capture", "transformation"]
    configuration: dict[str, Any]
    resources: tuple[Resource, ...] = ()


class Dependency(Fixed):
    label: Identifier
    binding_label: Identifier
    selection: Selector


class Request(Record, tag="request", kw_only=True):
    request_id: Identifier
    definition_id: Identifier
    inputs: tuple[InputBinding, ...]
    dependencies: tuple[Dependency, ...]


class Execution(Record, tag="execution", kw_only=True):
    execution_id: Identifier
    request_id: Identifier
    capture_origin: Origin | None = None


class ResultBinding(Fixed):
    label: Identifier
    entity_id: Identifier
    role: Literal["raw", "derived"]
    production: Literal["new", "adopted"]


class Outcome(Fixed):
    status: Literal["success", "failed", "interrupted", "incomplete"]
    value: Literal["outputs", "empty", "null"] | None = None
    outputs: tuple[ResultBinding, ...] = ()
    error: Identifier | None = None


class EntityEvent(Fixed):
    """The containing generation or usage field establishes the event's role."""

    event_id: Identifier
    entity_id: Identifier
    happened_at: str | None = None


class Derivation(Fixed):
    generated_entity_id: Identifier
    used_entity_id: Identifier
    generation_event_id: Identifier | None = None
    usage_event_id: Identifier | None = None


class Result(Record, tag="result", kw_only=True):
    """Only these actual relationships assert usage/generation/derivation.

    Events belong to execution_id; binding/dependency declarations do not
    fabricate events. Retention and current availability live in the ledger,
    independently of this immutable outcome.
    """

    result_id: Identifier
    execution_id: Identifier
    outcome: Outcome
    generations: tuple[EntityEvent, ...] = ()
    usages: tuple[EntityEvent, ...] = ()
    derivations: tuple[Derivation, ...] = ()


class Selection(Record, tag="selection", kw_only=True):
    selection_id: Identifier
    target: Origin
    request_id: Identifier
    selected_result_id: Identifier
    output_labels: tuple[Identifier, ...]


class RetentionPolicy(Record, tag="retention_policy", kw_only=True):
    policy_id: Identifier
    description: dict[str, Any]


class DependencyOmission(Fixed):
    scope: Literal["dependency", "resource"]
    label: Identifier
    reason: Identifier


class HistoricalDependencyObservation(Fixed):
    """An explicit retained assertion about a particular original result.

    Current input values alone are not an observation of historical inputs.
    The application supplying this receipt owns the truth of its assertion.
    """

    result_id: Identifier
    resource: Resource | None = None
    dependency: Dependency | None = None
    historical_input: InputBinding | None = None


class DependencySupplement(Fixed):
    reason: Identifier
    observation: HistoricalDependencyObservation
    supporting_entities: tuple[Identifier, ...]


class EvaluatedDependency(Fixed):
    dependency: Dependency
    binding: InputBinding
    selector: Selector
    evidence: ComparisonEvidence


class DependencyComparison(Fixed, kw_only=True):
    """Internal comparison evidence retained as an ordinary artifact entity."""

    format: Literal["docspec-dependency-comparison"] = "docspec-dependency-comparison"
    version: Literal[1] = 1
    result_id: Identifier
    evidence_version: Count
    definition: OperationDefinition
    dependencies: tuple[EvaluatedDependency, ...]
    evidence_ids: tuple[Identifier, ...]
    digest: Digest


class ReferencedDependencyComparison(Fixed, tag="DependencyComparison", kw_only=True):
    """Comparison evidence that refers to its already-retained definition.

    Corrected resources retain the historical effective values only when they
    differ from the original definition. None means use the original resources.
    """

    format: Literal["docspec-dependency-comparison"] = "docspec-dependency-comparison"
    version: Literal[2] = 2
    result_id: Identifier
    evidence_version: Count
    definition_id: Identifier
    definition_digest: Digest
    resources: tuple[Resource, ...] | None = None
    dependencies: tuple[EvaluatedDependency, ...]
    evidence_ids: tuple[Identifier, ...]
    digest: Digest


class DependencyEvidence(Record, tag="dependency_evidence", kw_only=True):
    """Append evidence; preserve the originally recorded dependency description."""

    evidence_id: Identifier
    result_id: Identifier
    status: Literal["omission", "supplement"]
    description: dict[str, Any]
    supersedes_evidence_id: Identifier | None = None


CoreRecord = (
    Entity | State | StateRepresentation | Revision | SelectedValue | OperationDefinition
    | Request | Execution | Result | Selection | RetentionPolicy | DependencyEvidence
)

# Index logical record identities without treating content digests or member
# keys as interchangeable IDs. Admission types remain the source of shape rules.
RECORD_ID_FIELDS = {
    "entity": "entity_id", "state": "state_id", "state_representation": "representation_id",
    "revision": "revision_id", "selected_value": "selected_value_id",
    "operation_definition": "definition_id", "request": "request_id", "execution": "execution_id",
    "result": "result_id", "selection": "selection_id", "retention_policy": "policy_id",
    "dependency_evidence": "evidence_id",
}
