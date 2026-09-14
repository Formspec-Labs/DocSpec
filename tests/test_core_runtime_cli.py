"""Python and command callers use the same retained Core workspace."""

from docspec.cli import main
from docspec.domain import core
from docspec.domain.core_admission import encode_record
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.runtime.core import CoreWorkspace


def write(path, value):
    path.write_bytes(canonical_value_bytes(value))
    return str(path)


def test_cli_create_revise_compare_select_and_reopen(tmp_path, capfdbinary):
    location = tmp_path / "workspace"
    source = tmp_path / "rows.jsonl"
    source.write_bytes(b'\n'.join(canonical_value_bytes({"key": key, "value": value}) for key, value in [("a", 1), ("b", "1")]))
    assert main(["state", "create", "--workspace", str(location), "--rows", str(source), "--state", "original"]) == 0
    capfdbinary.readouterr()
    with CoreWorkspace(location) as workspace:
        assert [(key, row.value.value) for key, row in workspace.rows("original")] == [("a", 1), ("b", "1")]
        revision = core.Revision(format_version=1, revision_id="r", base_state_id="original", result_state_id="revised",
                                 edits=(core.Remove(sequence=1, member_key="a"),))
    revision_path = tmp_path / "revision.json"
    revision_path.write_bytes(encode_record(revision))
    assert main(["state", "revise", "--workspace", str(location), "--revision", str(revision_path)]) == 0
    capfdbinary.readouterr()
    assert main(["compare", "--workspace", str(location), "original", "revised"]) == 0
    report = decode_canonical_json_value(capfdbinary.readouterr().out.rstrip(b'\n'))
    assert report["counts"] == {"added": 0, "changed": 0, "removed": 1}
    assert report["sample"][0]["member_key"] == "a"
    assert main(["select", "--workspace", str(location), "--unit", "choose", "--dataset", "data", "--target", "state", "revised"]) == 0
    capfdbinary.readouterr()
    assert main(["select", "--workspace", str(location), "--unit", "stale", "--dataset", "data", "--target", "state", "original"]) == 2
    assert b"StaleBaseError" in capfdbinary.readouterr().err
    with CoreWorkspace(location) as workspace:
        assert workspace.ledger.current("data") == ("state", "revised")
        assert workspace.compare("original", "revised", sample_limit=0)["sample"] == []


def produce(context):
    source = context.read_value("input")
    context.generate(core.InlineValue(value=source["x"] + 1), label="value")


def test_cli_execution_inspection_and_reuse_share_python_meaning(tmp_path, capfdbinary):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        workspace.retain([core.Entity(format_version=1, entity_id="input", entity_type="occurrence", value=core.InlineValue(value={"x": 2}))],
                         unit_id="input", roots=[("entity", "input")])
    definition = core.OperationDefinition(format_version=1, definition_id="add", implementation_id="add", implementation_version="1",
                                         operation_kind="transformation", configuration={})
    request = core.Request(format_version=1, request_id="request", definition_id="add", inputs=(core.WholeInput(label="input", entity_id="input"),),
                           dependencies=(core.Dependency(label="input", binding_label="input", selection=core.Whole()),))
    definition_path, request_path = tmp_path / "definition.json", tmp_path / "request.json"
    definition_path.write_bytes(encode_record(definition))
    request_path.write_bytes(encode_record(request))
    arguments = ["execute", "--workspace", str(tmp_path / "workspace"), "--definition", str(definition_path), "--request", str(request_path),
                 "--producer", "tests.test_core_runtime_cli:produce", "--target", "input", "--selection"]
    assert main([*arguments, "chosen"]) == 0
    first = decode_canonical_json_value(capfdbinary.readouterr().out.rstrip(b'\n'))
    assert main([*arguments, "reused"]) == 0
    second = decode_canonical_json_value(capfdbinary.readouterr().out.rstrip(b'\n'))
    assert second["result"] == first["result"]
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        report = workspace.inspect("selection", "reused")
        assert report["selected_result"]["record"]["result_id"] == first["result"]["result_id"]
        assert report["requested"]["record"]["request_id"] == "request"
        assert report["executed_request"]["record"]["request_id"] == "request"
        assert report["outputs"][0]["available"]


def test_document_commands_use_core_and_keep_source_cli(tmp_path, capfdbinary):
    from docspec.domain.content import CandidateFile, SourceItem
    location = tmp_path / "workspace"
    (tmp_path / "file.txt").write_text("Hello world")
    source = SourceItem("doc", "1", (CandidateFile("text", "file.txt", "text/plain"),))
    source_path = write(tmp_path / "sources.jsonl", source.to_dict())
    assert main(["document", "import", "--workspace", str(location), "--input-root", str(tmp_path), "--sources", source_path, "--state", "catalog"]) == 0
    capfdbinary.readouterr()
    assert main(["document", "run", "--workspace", str(location), "--input-root", str(tmp_path), "--source-state", "catalog", "--run-id", "run"]) == 0
    capfdbinary.readouterr()
    with CoreWorkspace(location) as workspace:
        assert len(list(workspace.rows("run"))) == 1
    from docspec.cli import build_parser
    help_text = build_parser().format_help()
    assert "source-catalog" in help_text
    assert "document-store" not in help_text
    assert "document-release" not in help_text


def test_result_inspection_reports_recorded_availability_without_reading_bulk_outputs(tmp_path, monkeypatch):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("state", [("key", {"x": 2})])
        occurrence = next(workspace.rows("state"))[1]
        definition = core.OperationDefinition(format_version=1, definition_id="adopt", implementation_id="adopt", implementation_version="1",
                                             operation_kind="transformation", configuration={})
        request = core.Request(format_version=1, request_id="adopt-request", definition_id="adopt", inputs=(core.WholeInput(label="input", entity_id=occurrence.entity_id),),
                               dependencies=(core.Dependency(label="input", binding_label="input", selection=core.Whole()),))
        result = workspace.operations.run(definition, request, lambda context: context.adopt(occurrence.entity_id, label="output"))
        def fail(*args, **kwargs):
            raise AssertionError("metadata inspection scanned bulk output payloads")
        monkeypatch.setattr(workspace.records, "lookup_batches", fail)
        report = workspace.inspect("result", result.result_id)
        assert report["outputs"] == [{"key": ["entity", occurrence.entity_id], "retained": True,
                                      "available": True, "evidence_version": 0, "record": None}]


def test_cli_export_opens_without_original_workspace(tmp_path, capfdbinary):
    from rulespec_artifacts import ArtifactPin, Producer
    from docspec.result_export import open_result_export
    implementation = "git+https://example.test/docspec@" + "1" * 40
    producer = Producer("docspec", implementation, "urn:test:core-export", "1", implementation)
    source = tmp_path / "workspace"
    with CoreWorkspace(source) as workspace:
        workspace.create("selected", [("a", 1), ("b", None)])
    producer_path = write(tmp_path / "producer.json", producer.as_dict())
    destination = tmp_path / "export"
    assert main(["export", "--workspace", str(source), "--state", "selected", "--destination", str(destination),
                 "--producer", producer_path, "--max-bytes", str(8 * 1024**2)]) == 0
    encoded_pin = decode_canonical_json_value(capfdbinary.readouterr().out.rstrip(b'\n'))
    pin = ArtifactPin(encoded_pin["logicalId"], encoded_pin["artifactDigest"])
    source.rename(tmp_path / "offline-workspace")
    with open_result_export(destination, expected_pin=pin, producer=producer, max_output_bytes=8 * 1024**2) as exported:
        assert [(key, entity.value.value) for key, entity in exported.rows()] == [("a", 1), ("b", None)]
