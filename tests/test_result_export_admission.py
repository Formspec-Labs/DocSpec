"""Import admission contract for Core exports: a valid artifact hash cannot excuse inconsistent imported
records, so resealing an export with recomputed member pins still refuses a changed state key, wrong byte size,
missing representation link, omitted root or altered provenance instant.

Also pins that an understated root count cannot bypass the payload read bound, explicit selection roots retain
original provenance, document-profile roots export complete stages after zero-work reuse, and a selected-field
export preserves parent identity without the parent's bytes.
"""

from contextlib import contextmanager, closing
import json
import sqlite3

import pytest
from rulespec_artifacts import (ArtifactPin, LocalMemberSource, Producer, MemberDescriptor, build_artifact_root,
    describe_member, stamp_root, write_member_manifest)

from docspec.adapters.result_export.writer import export_result
from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes, sha256_digest
from docspec.errors import IntegrityError, LimitExceededError
from docspec.runtime.core import CoreWorkspace
from tests.test_result_export import MAX_BYTES, PRODUCER, export, opened, retained as _retained_fixture


retained = _retained_fixture

def reseal(path):
    """Recompute every member pin and the artifact root from current bytes, returning a valid-looking signature."""
    source = LocalMemberSource(path)
    root = json.loads((path / "artifact.json").read_bytes())
    # Saved descriptors retain the role and filename; recompute every byte pin.
    values = [MemberDescriptor.from_dict(value, path="member") for value in json.loads((path / "members.json").read_bytes())["members"]]
    members = tuple(describe_member(source, object_key=value.object_key, role=value.role,
        media_type=value.media_type, record_count=value.record_count, schema_id=value.schema_id) for value in values)
    with (path / "members.json").open("wb") as stream:
        manifest = write_member_manifest(stream, scope_kind="global", scope_id="selected-core-state", object_key="members.json", members=members)
    root = build_artifact_root(kind=root["kind"], spec=root["spec"],
        producer=Producer.from_dict(root["producer"], path="producer"), manifests=(manifest,))
    (path / "artifact.json").write_bytes(canonical_value_bytes(root))
    return ArtifactPin(root["logicalId"], root["artifactDigest"])


def test_understated_root_cannot_bypass_payload_read_bound(retained, tmp_path, monkeypatch):
    workspace, _ = retained
    path = tmp_path / "export"
    export(workspace, path)
    root = json.loads((path / "artifact.json").read_bytes())
    root["counts"]["totalMemberByteSize"] = 0
    root = stamp_root(root)
    payload = canonical_value_bytes(root)
    (path / "artifact.json").write_bytes(payload)
    pin = ArtifactPin(root["logicalId"], root["artifactDigest"])
    limit = len(payload) + sum(value["byteSize"] for value in root["memberManifests"]) + 1
    calls = []
    original = LocalMemberSource.open
    @contextmanager
    def observed(self, key):
        calls.append(key)
        with original(self, key) as stream:
            yield stream
    monkeypatch.setattr(LocalMemberSource, "open", observed)
    with pytest.raises(LimitExceededError, match="max_output_bytes"):
        opened(path, pin, max_output_bytes=limit)
    assert calls == ["artifact.json"]


@pytest.mark.parametrize("mutation", ["key", "byte-size", "representation-link", "root-omitted"])
def test_resealed_inconsistent_core_identity_and_links_are_refused(retained, tmp_path, mutation):
    workspace, _ = retained
    path = tmp_path / "export"
    export(workspace, path)
    if mutation == "root-omitted":
        (path / "roots.jsonl").write_bytes(b"")
    else:
        with closing(sqlite3.connect(path / "ledger.sqlite")) as connection:
            if mutation == "key":
                value = json.loads(connection.execute("SELECT payload FROM records WHERE kind='state' AND record_id='selected'").fetchone()[0])
                value["state_id"] = "different-state"
                payload = canonical_value_bytes(value)
                connection.execute("UPDATE records SET payload=?,row_digest=?,byte_size=? WHERE kind='state' AND record_id='selected'",
                                   (payload, sha256_digest(payload), len(payload)))
            elif mutation == "byte-size":
                connection.execute("UPDATE records SET byte_size=1 WHERE kind='state'")
            else:
                connection.execute("DELETE FROM links WHERE relation='representation'")
            connection.commit()
    with pytest.raises(IntegrityError):
        opened(path, reseal(path))


def test_export_retains_original_provenance_from_explicit_selection_roots(tmp_path):
    path = tmp_path / "export"
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        definition = core.OperationDefinition(format_version=1, definition_id="definition", implementation_id="example", implementation_version="1",
                                              operation_kind="transformation", configuration={})
        request = core.Request(format_version=1, request_id="request", definition_id="definition", inputs=(), dependencies=())
        def produce(context):
            """Create the selected keyed state and generate its output record."""
            output = context.session.states.create_keyed(context.session, state_id="selected", representation_id="original", unit_id="import",
                rows=[("row", core.Entity(format_version=1, entity_id="row", entity_type="occurrence", value=core.InlineValue(value={"answer": 42})))])
            context.generate_record(output, label="output")
        # The target is independently retained input metadata for this example.
        workspace.create("target", [("source", {"v": 1})])
        first = workspace.operations.resolve(definition, request, produce, selection_id="first", target=core.Origin(parent_entity_id="target"), reuse_policy=lambda _: True)
        second = workspace.operations.resolve(definition, request, lambda _: pytest.fail("reused operation must not run"), selection_id="second",
                                              target=core.Origin(parent_entity_id="target"), reuse_policy=lambda _: True)
        assert first.result == second.result
        pin = export_result(workspace.publisher, workspace.records, "selected", path, producer=PRODUCER, max_output_bytes=MAX_BYTES,
                            additional_roots=iter([("selection", "second")]))
    with opened(path, pin) as view:
        assert view.record("result", first.result.result_id) == first.result
        assert view.record("selection", "second") == second.selection
        assert view.record("selection", "first") is None
        assert set(view.roots()) == {("state", "selected"), ("selection", "second")}
        assert view.summary["rootCount"] == 2
    with closing(sqlite3.connect(path / "ledger.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM provenance_events").fetchone()[0] > 0
        connection.execute("UPDATE provenance_events SET instant=1")
        connection.commit()
    with pytest.raises(IntegrityError, match="provenance"):
        opened(path, reseal(path))


def test_document_profile_roots_export_complete_stages_after_zero_work_reuse(tmp_path):
    from docspec.adapters.content_fetchers import LocalFileContentFetcher
    from docspec.application.document_processors import content_statistics_processor
    from docspec.domain.content import CandidateFile, SourceItem
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "document.txt").write_text("First paragraph.\n\nSecond paragraph.")
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(sources))
        pipeline.import_sources([SourceItem("document", "1", (CandidateFile("text", "document.txt", "text/plain"),))], state_id="catalog")
        pipeline.run("catalog", run_id="first", processors=(content_statistics_processor(),))
        from docspec.application.document_run import run_request_id
        with workspace.ledger._transaction() as connection:
            before = {row[0] for row in connection.execute("SELECT record_id FROM records WHERE kind='execution'")}
        pipeline.run("catalog", run_id="successor", processors=(content_statistics_processor(),))
        with workspace.ledger._transaction() as connection:
            after = {row[0] for row in connection.execute("SELECT record_id FROM records WHERE kind='execution'")}
        controlling = {identity for batch in workspace.ledger.executions(run_request_id("successor")) for identity in batch}
        assert after - before == controlling and len(controlling) == 1
        roots = list(pipeline.retained_roots("successor"))
        selections = [next(workspace.ledger.read_records([key]))[0].value for key in roots if key[0] == "selection"]
        results = [next(workspace.ledger.read_records([("result", choice.selected_result_id)]))[0].value for choice in selections]
        pin = workspace.export("successor", tmp_path / "export", producer=PRODUCER, max_output_bytes=MAX_BYTES,
                               additional_roots=pipeline.retained_roots("successor"))
    workspace.path.rename(tmp_path / "original-unavailable")
    with opened(tmp_path / "export", pin) as view:
        assert set(view.roots()) == set(roots)
        for result in results:
            assert view.record("result", result.result_id) == result
            for output in result.outcome.outputs:
                entity = view.record("entity", output.entity_id)
                if entity is None:
                    assert list(view.rows(output.entity_id))
                elif isinstance(entity.value, core.ContentRef):
                    from docspec.domain.references import BlobRef
                    value = entity.value
                    assert view.read_blob(BlobRef(value.locator, value.digest, value.byte_size, value.media_type), max_bytes=MAX_BYTES)


def test_selected_fields_export_preserves_parent_identity_without_parent_bytes(tmp_path, monkeypatch):
    from docspec.ports.core_ledger import MetadataBatch
    from docspec.domain.references import BlobRef
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        workspace.create("selected", [("summary", {"answer": 1})])
        with workspace.publisher.session() as session:
            value = session.retain_value({"public": 1, "private": "whole parent bytes are not selected"})
            parent = core.Entity(format_version=1, entity_id="parent", entity_type="artifact", value=value)
            session.publish(MetadataBatch("parent", records=(parent,), retained=(("entity", "parent"),)))
            fields = workspace.selections.retain(session, selected_value_id="public-field", origin=core.Origin(parent_entity_id="parent"),
                definition=core.JsonFields(selectors=(core.Field(label="public", pointer="/public"),)))
        read = workspace.blobs.read
        def refuse_parent(reference, **kwargs):
            """Fail if the direct selected-field export reads the original whole-parent blob."""
            if reference.digest == value.digest:
                pytest.fail("direct selected-field export must not read the original whole parent")
            yield from read(reference, **kwargs)
        monkeypatch.setattr(workspace.blobs, "read", refuse_parent)
        pin = workspace.export("selected", tmp_path / "export", producer=PRODUCER, max_output_bytes=MAX_BYTES,
                               additional_roots=[("selected_value", "public-field")])
    with opened(tmp_path / "export", pin) as view:
        assert view.record("selected_value", "public-field") == fields
        assert view.record("selected_value", "public-field").origin.parent_entity_id == "parent"
        assert view.record("entity", "parent") is None
        assert not (tmp_path / "export" / "blobs" / value.locator).exists()
        with pytest.raises(IntegrityError, match="does not belong"):
            view.read_blob(BlobRef(value.locator, value.digest, value.byte_size, value.media_type), max_bytes=MAX_BYTES)
