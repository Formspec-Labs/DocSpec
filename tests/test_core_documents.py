"""Document work uses the same retained Core lifecycle after reopening."""

import pytest

from docspec.adapters.content_fetchers.local_file import LocalFileContentFetcher
from docspec.domain.content import CandidateFile, SourceItem
from docspec.processing.extraction import TextExtractor
from docspec.runtime.core import CoreWorkspace


def source(title="old"):
    return SourceItem("document", "1", (CandidateFile("text", "file.txt", "text/plain"),), metadata={"title": title})


def selected(workspace, pipeline, state_id):
    summary = list(pipeline.rows(state_id))[0][2]
    selections = [row.value for batch in workspace.ledger.read_records(("selection", key) for key in summary["selections"]) for row in batch]
    return [row.value for batch in workspace.ledger.read_records(("result", selection.selected_result_id) for selection in selections) for row in batch]


def test_capture_later_processing_metadata_reuse_reopen_and_alternatives(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_text("Hello world.\n\nSecond paragraph.")
    fetcher = LocalFileContentFetcher(inputs)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=fetcher)
        pipeline.import_sources([source()], state_id="catalog")
        pipeline.run("catalog", run_id="capture", extract=False, segment=False, dataset="documents")
        first = selected(workspace, pipeline, "capture")
        assert len(first) == 1 and first[0].outcome.status == "success"
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=fetcher)
        pipeline.run("catalog", run_id="processed", dataset="documents")
        processed = selected(workspace, pipeline, "processed")
        assert len(processed) == 3 and processed[0] == first[0]
        pipeline.import_sources([source("retitled")], state_id="new-catalog")
        pipeline.run("new-catalog", run_id="retitled")
        assert selected(workspace, pipeline, "retitled") == processed
        pipeline.run("new-catalog", run_id="alternative", fresh=True)
        assert all(a.result_id != b.result_id for a, b in zip(processed, selected(workspace, pipeline, "alternative"), strict=True))
        assert workspace.ledger.current("documents") == ("state", "processed")
        assert not (workspace.path / "controlRepository").exists()


def test_failed_extraction_repairs_without_recapture(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_text("Hello world")
    class Flaky(TextExtractor):
        broken = True
        def extract(self, *args):
            if self.broken:
                raise RuntimeError("temporary extraction failure")
            return super().extract(*args)
    extractor = Flaky()
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs), extractor=extractor)
        pipeline.import_sources([source()], state_id="catalog")
        with pytest.raises(RuntimeError, match="temporary"):
            pipeline.run("catalog", run_id="failed")
        extractor.broken = False
        pipeline.run("catalog", run_id="repaired")
        with workspace.ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='execution'").fetchone() == (6,)


def test_processor_graph_uses_selected_prerequisites_and_common_reuse(tmp_path):
    from docspec.application.documents import DocumentProcessor
    from docspec.domain import core
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_text("A short document")
    calls = []
    def definition(name):
        return core.OperationDefinition(format_version=1, definition_id=name, implementation_id=name,
            implementation_version="1", operation_kind="transformation", configuration={})
    def first(context, inputs):
        calls.append("first")
        context.use(inputs["segments"].state_id)
        context.generate(core.InlineValue(value={"answer": 3}), label="value")
    def second(context, inputs):
        calls.append("second")
        value = context.read_value(inputs["first:value"])
        context.generate(core.InlineValue(value=value["answer"] + 1), label="value")
    processors = (DocumentProcessor("second", definition("second"), second, ("first",)),
                  DocumentProcessor("first", definition("first"), first))
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
        pipeline.import_sources([source()], state_id="catalog")
        pipeline.run("catalog", run_id="first", processors=processors)
        pipeline.run("catalog", run_id="second", processors=processors)
        assert calls == ["first", "second"]
        assert len(selected(workspace, pipeline, "second")) == 5


def test_bulk_segments_and_statistics_preserve_positions_and_evidence(tmp_path):
    from docspec.application.document_processors import content_statistics_processor
    from docspec.processing.segmentation import ParagraphSegmenter
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    count = 2050
    (inputs / "file.txt").write_text("\n\n".join(f"Paragraph {index}" for index in range(count)))
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs), segmenter=ParagraphSegmenter())
        pipeline.import_sources([source()], state_id="catalog")
        processor = content_statistics_processor()
        pipeline.run("catalog", run_id="processed", processors=(processor,))
        definition = next(workspace.ledger.read_records([("operation_definition", processor.definition.definition_id)]))[0]
        assert definition.value.configuration["segmentOrder"] == {
            "key": "member_key", "direction": "ASC", "collation": "binary",
            "missingKeys": "forbidden", "duplicateKeys": "forbidden"}
        outcomes = selected(workspace, pipeline, "processed")
        segments_id = outcomes[2].outcome.outputs[0].entity_id
        stats_id = outcomes[3].outcome.outputs[0].entity_id
        with workspace.publisher.session() as session:
            with workspace.states.relation(session, segments_id) as relation:
                assert relation.aggregate("count(*)").fetchone() == (count * 2,)
        statistics = list(pipeline.rows(stats_id))
        assert len(statistics) == count
        assert statistics[0][0] == "000000000000:metadata"
        assert statistics[-1][0] == "000000002049:metadata"
        assert all(value["wordCount"] == 2 and value["evidence"] for _, _, value in statistics)
        assert len(outcomes[2].generations) == 1
        assert len(outcomes[3].generations) == 1


def test_documents_batch_lookup_publication_and_survive_sibling_failure(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_text("Reusable source")
    fetcher = LocalFileContentFetcher(inputs)
    calls, lookups, publications = [], [], []
    original_fetch = fetcher.fetch
    broken = True

    def fetch(candidate, **kwargs):
        calls.append(candidate.candidate_id)
        if broken and candidate.candidate_id == "body-05":
            raise RuntimeError("one broken sibling")
        return original_fetch(candidate, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", fetch)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=fetcher)
        items = [SourceItem(f"document-{index:02d}", "1", (
            CandidateFile(f"body-{index:02d}", "file.txt", "text/plain"),)) for index in range(40)]
        pipeline.import_sources(items, state_id="catalog")
        find, publish = workspace.ledger.find_candidates, pipeline.operations.publish

        def measured_find(requests):
            requests = tuple(requests)
            lookups.append(len(requests))
            yield from find(requests)

        def measured_publish(pending, **kwargs):
            pending = tuple(pending)
            if pending:
                publications.append(len(pending))
            return publish(pending, **kwargs)

        monkeypatch.setattr(workspace.ledger, "find_candidates", measured_find)
        monkeypatch.setattr(pipeline.operations, "publish", measured_publish)
        with pytest.raises(RuntimeError, match="broken sibling"):
            pipeline.run("catalog", run_id="failed", extract=False, segment=False, dataset="docs")
        assert workspace.ledger.current("docs") is None
        assert publications == [5]
        assert lookups == [32]
        broken = False
        pipeline.run("catalog", run_id="repaired", extract=False, segment=False, dataset="docs")
        assert all(calls.count(f"body-{index:02d}") == (2 if index == 5 else 1) for index in range(40))
        assert publications == [5, 27, 8, 1]
        assert lookups == [32, 32, 8]
        assert len(list(pipeline.rows("repaired"))) == 40
        before = list(calls)
        pipeline.run("catalog", run_id="reused", extract=False, segment=False)
        assert calls == before
        assert lookups[-2:] == [32, 8]
        assert workspace.ledger.current("docs") == ("state", "repaired")


def test_document_roots_include_inactive_sources_and_candidate_order(tmp_path):
    from docspec.domain.content import SourceItemState
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_text("Two formats")
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
        pipeline.import_sources([
            SourceItem("active", "1", (CandidateFile("first", "file.txt", "text/plain"),
                CandidateFile("second", "file.txt", "text/plain"))),
            SourceItem("inactive", "1", (), state=SourceItemState.DELETED),
        ], state_id="catalog")
        pipeline.run("catalog", run_id="run", segment=False)
        rows = list(pipeline.rows("run"))
        active, inactive = rows[0][2], rows[1][2]
        assert inactive["selections"] == []
        results = selected(workspace, pipeline, "run")
        assert [value.capture_origin is not None for value in (
            next(workspace.ledger.read_records([("execution", result.execution_id)]))[0].value for result in results
        )] == [True, False, True, False]
        roots = list(pipeline.retained_roots("run"))
        assert roots[1][0] == "result"
        assert workspace.inspect(*roots[1])["record"]["outcome"]["status"] == "success"
        assert [roots[0], *roots[2:]] == [("state", "run"), ("entity", active["sourceEntityId"]),
            *(("selection", identity) for identity in active["selections"]), ("entity", inactive["sourceEntityId"])]


def test_invocation_limits_charge_new_work_only_and_leave_failures_authoritative(tmp_path):
    from docspec.application.documents import DocumentProcessor
    from docspec.domain import core
    from docspec.errors import LimitExceededError
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_bytes(b"Hello")
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
        pipeline.import_sources([source()], state_id="catalog")
        with pytest.raises(LimitExceededError, match="source byte limit"):
            pipeline.run("catalog", run_id="too-many-bytes", max_source_bytes=4)
        with pytest.raises(LimitExceededError, match="generated row limit"):
            pipeline.run("catalog", run_id="too-many-rows", max_source_bytes=5, max_generated_rows=1)
        # Capture and extraction succeeded before segmentation exceeded its
        # two-row budget. Repair uses those exact results without fetching.
        pipeline.run("catalog", run_id="repaired", max_source_bytes=0, max_generated_rows=2)
        calls = []
        def empty(context, inputs):
            context.use(inputs["segments"].state_id)
            calls.append(True)
        processor = DocumentProcessor("empty", core.OperationDefinition(format_version=1, definition_id="empty",
            implementation_id="empty", implementation_version="1", operation_kind="transformation", configuration={}), empty)
        pipeline.run("catalog", run_id="zero-new-rows", max_source_bytes=0, max_generated_rows=0, processors=(processor,))
        assert calls == [True]
        outcomes = selected(workspace, pipeline, "zero-new-rows")
        assert len(outcomes) == 4 and outcomes[-1].outcome.value == "empty"
        with workspace.ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='result' AND json_extract(payload, '$.outcome.status')='failed'").fetchone() == (2,)
