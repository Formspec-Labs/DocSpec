"""Provider adapters preserve policy/value checks without old request wrappers."""

from dataclasses import replace

import pytest

from docspec.adapters.content_fetchers.local_file import LocalFileContentFetcher
from docspec.adapters.document_processor import provider_processor
from docspec.domain import core
from docspec.domain.identity import sha256_digest
from docspec.domain.processor_policy import (DataUsePolicy, ProcessorExecutionScope, ProcessorLimits, ProcessorResponse,
    ProviderEvidence, ProviderInteractionEvidence)
from docspec.errors import IntegrityError, LimitExceededError, SchemaValidationError
from docspec.runtime.core import CoreWorkspace
from tests.test_core_documents import source, selected


def setup(tmp_path, callback, *, policy=None, limits=ProcessorLimits()):
    """Build a workspace, document pipeline and provider processor over one local text input."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "file.txt").write_text("Hello world")
    workspace = CoreWorkspace(tmp_path / "workspace")
    pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
    pipeline.import_sources([source()], state_id="catalog")
    definition = core.OperationDefinition(format_version=1, definition_id="provider", implementation_id="fixture",
        implementation_version="1", operation_kind="transformation", configuration={})
    processor = provider_processor("provider", definition=definition, process=callback,
        data_use_policy=policy or DataUsePolicy.create(execution_scope=ProcessorExecutionScope.LOCAL_ONLY,
            allowed_fields=("content", "evidence")), output_schema={"type": "object", "required": ["answer"],
            "properties": {"answer": {"type": "integer"}}, "additionalProperties": False}, limits=limits)
    return workspace, pipeline, processor


def test_allowed_fields_external_evidence_and_output_schema_are_retained(tmp_path):
    """Only the allowed field reaches the provider; its external evidence is retained and reuse does not recall it."""
    evidence = ProviderInteractionEvidence("fixture", ProviderEvidence.digest_only(sha256_digest(b"request")),
                                          ProviderEvidence.digest_only(sha256_digest(b"response")))
    calls = []
    def provider(value):
        calls.append(value)
        assert set(value) == {"evidence"}
        return ProcessorResponse(({"answer": 42},), "application/json", provider_evidence=evidence, external_request_count=1)
    policy = DataUsePolicy.create(execution_scope=ProcessorExecutionScope.DECLARED_EXTERNAL, allowed_fields=("evidence",))
    workspace, pipeline, processor = setup(tmp_path, provider, policy=policy)
    with workspace:
        pipeline.run("catalog", run_id="run", processors=(processor,))
        result = selected(workspace, pipeline, "run")[-1]
        rows = list(pipeline.rows(result.outcome.outputs[0].entity_id))
        receipt = next(value for key, _, value in rows if key.endswith(":receipt"))
        assert receipt["providerEvidence"] == evidence.to_dict()
        assert receipt["externalRequestCount"] == 1
        assert next(value for key, _, value in rows if ":output:" in key) == {"answer": 42}
        pipeline.run("catalog", run_id="reuse", processors=(processor,))
        assert len(calls) == 1


@pytest.mark.parametrize("failure", ["schema", "media", "resources", "external", "count", "bytes", "input"])
def test_invalid_provider_output_fails_the_common_attempt(tmp_path, failure):
    """Every output-schema, media, resource, external-call, count, byte or input violation fails the attempt."""
    response = ProcessorResponse(({"answer": 1},), "application/json")
    limits = ProcessorLimits()
    if failure == "schema":
        response = replace(response, values=({"answer": "wrong"},))
    if failure == "media":
        response = replace(response, media_type="text/plain")
    if failure == "resources":
        response = replace(response, resources=({"unexpected": True},))
    if failure == "external":
        response = replace(response, external_request_count=1)
    if failure == "count":
        response, limits = replace(response, values=({"answer": 1},) * 2), ProcessorLimits(max_output_records=1)
    if failure == "bytes":
        limits = ProcessorLimits(max_output_bytes=1)
    if failure == "input":
        limits = ProcessorLimits(max_input_bytes=1)
    workspace, pipeline, processor = setup(tmp_path, lambda value: response, limits=limits)
    with workspace, pytest.raises((IntegrityError, LimitExceededError, SchemaValidationError)):
        pipeline.run("catalog", run_id="run", processors=(processor,))


def test_order_sensitive_provider_retains_the_shared_consumed_order_after_reopen(tmp_path):
    """Segment consumption order is retained across reopen as binary ascending member keys, with gaps and duplicates forbidden."""
    from docspec.processing.segmentation import ParagraphSegmenter

    calls = []
    def provider(value):
        calls.append((value["segmentOrdinal"], value["content"]))
        return ProcessorResponse(({"answer": len(calls)},), "application/json")
    policy = DataUsePolicy.create(execution_scope=ProcessorExecutionScope.LOCAL_ONLY,
                                  allowed_fields=("content", "segmentOrdinal"))
    workspace, pipeline, processor = setup(tmp_path, provider, policy=policy)
    (tmp_path / "inputs" / "file.txt").write_text("First\n\nSecond\n\nThird")
    pipeline.segmenter = ParagraphSegmenter()
    with workspace:
        pipeline.run("catalog", run_id="run", processors=(processor,))
        assert calls == [(0, b"First"), (1, b"Second"), (2, b"Third")]
        result = selected(workspace, pipeline, "run")[-1]
        assert [value["answer"] for key, _, value in pipeline.rows(result.outcome.outputs[0].entity_id)
                if ":output:" in key] == [1, 2, 3]
        pipeline.run("catalog", run_id="reuse", processors=(processor,))
        assert len(calls) == 3
    with CoreWorkspace(tmp_path / "workspace") as reopened:
        definition = next(reopened.ledger.read_records([("operation_definition", processor.definition.definition_id)]))[0]
        assert definition.retained
        assert definition.value.configuration["documentProcessor"]["segmentOrder"] == {
            "key": "member_key", "direction": "ASC", "collation": "binary",
            "missingKeys": "forbidden", "duplicateKeys": "forbidden"}
