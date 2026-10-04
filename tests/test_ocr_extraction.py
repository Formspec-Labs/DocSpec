"""OCR observations use the same retained execution and recovery as ordinary text."""

from hashlib import sha256

import pytest
from spicy_docs.extraction.model import Box, Observation, PageContent, PageResult, TextBlock

from docspec.adapters.content_fetchers.local_file import LocalFileContentFetcher
from docspec.domain.content import CandidateFile, SourceItem
from docspec.processing.ocr import OcrExtractor
from docspec.runtime.core import CoreWorkspace


class Pages:
    calls = 0
    broken = False
    truncated = False

    def extract(self, source, *, media_type, pages):
        self.calls += 1
        for number in pages or (1, 2):
            if number == 2 and self.broken:
                raise RuntimeError("temporary OCR failure")
            if number == 2 and self.truncated:
                return
            text = f"Read page {number}"
            blocks = (TextBlock(text, Box(0.1, 0.2, 0.8, 0.4), confidence=0.95),)
            observation = Observation("ocr", text, {"model": "fixture"}, {"response": text, "bytes": b"raw"}, blocks)
            yield PageResult(
                {"page": number, "page_count": 2, "source_sha256": sha256(source).hexdigest()},
                PageContent(blocks, (observation,)),
            )


def setup(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "scan.pdf").write_bytes(b"retained scanned PDF fixture")
    provider = Pages()
    extractor = OcrExtractor(provider, processing_identity={"reader": "fixture", "model": "fixture/v1"})
    return inputs, provider, extractor


def results(workspace, pipeline, state):
    row = list(pipeline.rows(state))[0][2]
    selections = [
        record.value
        for batch in workspace.ledger.read_records(("selection", key) for key in row["selections"])
        for record in batch
    ]
    return [
        record.value
        for batch in workspace.ledger.read_records(("result", item.selected_result_id) for item in selections)
        for record in batch
    ]


def test_ocr_retains_provider_bytes_page_mapping_and_reuses_after_reopen(tmp_path):
    inputs, provider, extractor = setup(tmp_path)
    source = SourceItem("scan", "1", (CandidateFile("pdf", "scan.pdf", "application/pdf"),))
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs), extractor=extractor)
        pipeline.import_sources([source], state_id="catalog")
        pipeline.run("catalog", run_id="read")
        original = results(workspace, pipeline, "read")
        extraction = original[1]
        outputs = {output.label: output.entity_id for output in extraction.outcome.outputs}
        assert "ocr-observations" in outputs and "ocr-bytes-000000" in outputs
        with workspace.publisher.session() as session:
            metadata = next(
                record.value.value.value
                for batch in session.read_records([("entity", outputs["representation"])])
                for record in batch
            )
            assert metadata["receipt"]["metadata"]["complete"] is True
            assert [item["evidence"]["page"] for item in metadata["representation"]["evidenceMappings"]] == [1, 2]
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs), extractor=extractor)
        pipeline.run("catalog", run_id="reuse")
        assert results(workspace, pipeline, "reuse") == original
        assert provider.calls == 1


def test_ocr_failure_retry_reuses_capture_and_refuses_truncation(tmp_path):
    inputs, provider, extractor = setup(tmp_path)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs), extractor=extractor)
        pipeline.import_sources(
            [SourceItem("scan", "1", (CandidateFile("pdf", "scan.pdf", "application/pdf"),))], state_id="catalog"
        )
        provider.broken = True
        with pytest.raises(RuntimeError, match="temporary OCR failure"):
            pipeline.run("catalog", run_id="failed")
        with workspace.ledger._transaction() as connection:
            failure_ids = [
                row[0]
                for row in connection.execute("SELECT record_id FROM records WHERE kind='result' AND outcome='failed'")
            ]
        failure = next(
            record.value
            for batch in workspace.ledger.read_records([("result", item) for item in failure_ids])
            for record in batch
            if any(output.label == "ocr-failure" for output in record.value.outcome.outputs)
        )
        output = next(output for output in failure.outcome.outputs if output.label == "ocr-failure")
        with workspace.publisher.session() as session:
            entity = next(
                record.value for batch in session.read_records([("entity", output.entity_id)]) for record in batch
            )
            from docspec.domain.references import BlobRef
            import json

            value = entity.value
            record = json.loads(
                b"".join(workspace.blobs.read(BlobRef(value.locator, value.digest, value.byte_size, value.media_type)))
            )
            assert record["complete"] is False
            assert record["pages"][0]["content"]["blocks"][0]["confidence"] == 0.95
        assert not any(output.label == "representation" for output in failure.outcome.outputs)
        provider.broken = False
        provider.truncated = True
        with pytest.raises(ValueError, match="before all requested"):
            pipeline.run("catalog", run_id="truncated")
        provider.truncated = False
        pipeline.run("catalog", run_id="repaired")
        assert provider.calls == 3
        assert len(results(workspace, pipeline, "repaired")) == 3


def test_retained_observations_verify_without_model_replay(tmp_path):
    from tests.support.processing import _captured
    from docspec.processing.artifacts import verify_representation_evidence
    from docspec.errors import IntegrityError

    _, provider, extractor = setup(tmp_path)
    content = b"source scan"
    result = extractor.extract(_captured(content, "application/pdf"), content)
    verify_representation_evidence(result.payload, content, derived_resolver=extractor.evidence_resolver(result))
    assert provider.calls == 1
    with pytest.raises(IntegrityError):
        verify_representation_evidence(
            result.payload, b"other source", derived_resolver=extractor.evidence_resolver(result)
        )


def test_processing_identity_changes_selection_and_provider_objects_refuse(tmp_path):
    from tests.support.processing import _captured

    _, provider, extractor = setup(tmp_path)
    changed = OcrExtractor(provider, processing_identity={"model": "other"})
    assert changed.configuration_digest != extractor.configuration_digest

    class ObjectPages(Pages):
        def extract(self, *args, **kwargs):
            for page in super().extract(*args, **kwargs):
                page.content.observations[0].raw["object"] = object()
                yield page

    bad = OcrExtractor(ObjectPages(), processing_identity={"model": "fixture"})
    with pytest.raises((ValueError, TypeError)):
        bad.extract(_captured(b"source", "application/pdf"), b"source")


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
@pytest.mark.parametrize("selection", [None, (1,)])
def test_invalid_page_count_never_completes(count, selection):
    from tests.support.processing import _captured

    class InvalidPages(Pages):
        def extract(self, *args, **kwargs):
            for result in super().extract(*args, **kwargs):
                result.metadata["page_count"] = count
                yield result

    extractor = OcrExtractor(InvalidPages(), processing_identity={"model": "fixture"}, pages=selection)
    with pytest.raises(ValueError, match="invalid page count") as error:
        extractor.extract(_captured(b"source", "application/pdf"), b"source")
    assert any(item.label == "ocr-failure" for item in error.value.extraction_evidence)


def test_plain_iterator_needs_no_close_method():
    from tests.support.processing import _captured

    class IteratorPages(Pages):
        def extract(self, *args, **kwargs):
            return iter(list(super().extract(*args, **kwargs)))

    result = OcrExtractor(IteratorPages(), processing_identity={"model": "fixture"}).extract(
        _captured(b"source", "application/pdf"), b"source"
    )
    assert result.receipt.metadata["complete"] is True
