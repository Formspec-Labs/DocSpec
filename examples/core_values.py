"""Revise two fields, reuse only unaffected work, and recover exact choices.

Run with: python -m examples.core_values /path/to/new-workspace
"""

import argparse
from contextlib import closing
from pathlib import Path

from docspec.application.core_edits import prepare_value_edit
from docspec.domain import core
from docspec.domain.identity import canonical_json_file_bytes
from docspec.runtime import CoreWorkspace


DEFINITION = core.OperationDefinition(
    format_version=1, definition_id="example:url-operation", implementation_id="example.url",
    implementation_version="1", operation_kind="transformation", configuration={},
)


def run(path: Path) -> dict:
    executions = []

    def resolve(workspace, identity, entity_id):
        request = core.Request(
            format_version=1, request_id=identity, definition_id=DEFINITION.definition_id,
            inputs=(core.WholeInput(label="document", entity_id=entity_id),),
            dependencies=(core.Dependency(label="url", binding_label="document",
                selection=core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),))),),
        )

        def produce(context):
            executions.append(context.execution.execution_id)
            document = context.read_value(entity_id)
            output = context.generate(core.InlineValue(value={"url": document["url"]}), label="url")
            context.derive(output.entity_id, entity_id,
                           generation_event_id=context.generations[-1].event_id,
                           usage_event_id=context.usages[-1].event_id)

        return workspace.operations.resolve(
            DEFINITION, request, produce, selection_id=identity + ":selection",
            target=core.Origin(parent_entity_id=entity_id), reuse_policy=lambda result: True,
        )

    def revise(workspace, state, entity, field, value, target):
        prepared, edit = prepare_value_edit(workspace.operations, entity,
                                            [{"op": "replace", "path": "/" + field, "value": value}])
        workspace.operations.publish([prepared])
        workspace.revise(core.Revision(
            format_version=1, revision_id=target + ":revision", base_state_id=state, result_state_id=target,
            edits=(core.Put(sequence=0, member_key="notice", occurrence_id=edit.result_occurrence_id),),
            value_edits=(edit,),
        ))
        return edit.result_occurrence_id

    with CoreWorkspace(path) as workspace:
        workspace.create("original", [("notice", {"url": "https://example.org/a", "title": "Original"})])
        with closing(workspace.rows("original")) as rows:
            original_id = next(rows)[1].entity_id
        original = resolve(workspace, "original-request", original_id)
        retitled_id = revise(workspace, "original", original_id, "title", "Retitled", "retitled")
        retitled = resolve(workspace, "retitled-request", retitled_id)
        changed_id = revise(workspace, "retitled", retitled_id, "url", "https://example.org/b", "changed")
        changed = resolve(workspace, "changed-request", changed_id)
        assert retitled.result == original.result
        assert changed.result != original.result
        assert len(executions) == 2

    with CoreWorkspace(path) as workspace:
        recovered = resolve(workspace, "retitled-request", retitled_id)
        assert recovered == retitled and len(executions) == 2
        return {
            "producer_executions": len(executions),
            "title_change_reused": retitled.result.result_id == original.result.result_id,
            "url_change_executed": changed.result.result_id != original.result.result_id,
            "reopened_exact_selection": recovered.selection.selection_id,
            "original_execution": original.result.execution_id,
            "retitled_execution": retitled.result.execution_id,
            "comparison": workspace.compare("original", "changed"),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    print(canonical_json_file_bytes(run(parser.parse_args().workspace)).decode(), end="")
