"""Shared small Core operations for scheduler portability checks."""

from docspec.domain import core
from docspec.adapters.dagster import ScheduledOperation
from docspec.ports.core_ledger import MetadataBatch


def operations(count=2, *, prefix="job", fresh=False):
    """Yield small scheduled operations over one configured input, fresh or reusable by request."""

    for index in range(count):
        definition = core.OperationDefinition(format_version=1, definition_id=f"definition:{index}",
            implementation_id="fixture.double", implementation_version="1", operation_kind="transformation",
            configuration={"input": f"input:{index}"})
        request = core.Request(format_version=1, request_id=f"{prefix}:request:{index}", definition_id=definition.definition_id,
            inputs=(core.WholeInput(label="input", entity_id=f"input:{index}"),),
            dependencies=(core.Dependency(label="input", binding_label="input", selection=core.Whole()),))
        yield ScheduledOperation(definition=definition, request=request, selection_id=f"{prefix}:selection:{index}",
                                 target=core.Origin(parent_entity_id=f"input:{index}"), fresh=fresh)


def seed(workspace, count=2):
    """Publish the inline input entities the fixture operations read."""
    values = tuple(core.Entity(format_version=1, entity_id=f"input:{index}", entity_type="artifact", value=core.InlineValue(value=index + 1))
                   for index in range(count))
    with workspace.publisher.session() as session:
        session.publish(MetadataBatch("inputs", records=values, retained=tuple(("entity", value.entity_id) for value in values)))


def resolver(definition):
    """Return a producer that doubles the definition's configured input as ``double``."""
    def produce(context):
        context.generate(core.InlineValue(value=context.read_value(definition.configuration["input"]) * 2), label="double")
    return produce


def result_values(workspace, choices):
    """Return the sorted integer outputs each selection's result entity carries."""
    values = []
    for selection in choices:
        result = next(workspace.ledger.read_records([("result", selection.selected_result_id)]))[0].value
        entity_id = result.outcome.outputs[0].entity_id
        values.append(next(workspace.ledger.read_records([("entity", entity_id)]))[0].value.value.value)
    return sorted(values)

