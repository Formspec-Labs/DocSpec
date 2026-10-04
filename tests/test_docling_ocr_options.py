"""Model choices bind retained results to their selected runtime and artifact bytes."""

from contextlib import contextmanager
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from docspec.adapters.docling_ocr import DOCLING_OCR_MODELS, create_docling_ocr_extractor
from docspec.processing.artifacts import verify_representation_evidence
from tests.support.processing import _captured


@pytest.fixture
def local_provider(monkeypatch):
    from spicy_docs.extraction import DefaultReader, Docling, DoclingVlmSettings, Raster, Recognition

    state = {"prepared": [], "calls": [], "weightDigest": "first-weights"}

    def prepare(settings, *, local_files_only):
        state["prepared"].append((settings.model, local_files_only))
        return {"revision": settings.model, "files": {"model.safetensors": state["weightDigest"]}}

    def configuration(backend):
        return {
            "model": backend.settings.model,
            "maxTokens": backend.settings.max_tokens,
            "imageScale": backend.settings.image_scale,
            "timeout": backend.settings.document_timeout,
            "modelIdentity": backend.model_identity,
        }

    def recognize(backend, image):
        state["calls"].append(backend.settings.model)
        config = configuration(backend)
        text = f"Read with {backend.settings.model}"
        return Recognition(text, config, {"text": text, "configuration": config})

    @contextmanager
    def open_page(reader, content, media_type):
        page = SimpleNamespace(geometry={}, render=lambda: Raster(b"fixture image", 1, 1))
        yield SimpleNamespace(page_count=1, page=lambda number: page)

    monkeypatch.setattr(DoclingVlmSettings, "check_platform", lambda settings: None)
    monkeypatch.setattr(DoclingVlmSettings, "prepare_artifacts", prepare)
    monkeypatch.setattr(Docling, "configuration", configuration)
    monkeypatch.setattr(Docling, "recognize", recognize)
    monkeypatch.setattr(DefaultReader, "open", open_page)
    monkeypatch.setattr("docspec.adapters.docling_ocr.version", lambda package: "fixture-1")
    return state


@pytest.mark.parametrize("model", DOCLING_OCR_MODELS)
def test_model_option_retains_provider_settings_and_replayable_page_evidence(model, tmp_path, local_provider):
    extractor = create_docling_ocr_extractor(model, artifacts_path=tmp_path, pages=(1,), local_files_only=True)
    source = b"source page fixture"
    result = extractor.extract(_captured(source, "application/pdf"), source)
    assert result.payload.content == f"Read with {model}".encode()
    observations = json.loads(next(item.content for item in result.evidence if item.label == "ocr-observations"))
    provider = observations["pages"][0]["content"]["observations"][0]
    assert provider["configuration"]["model"] == model
    assert provider["configuration"]["maxTokens"] == 16_384
    assert provider["configuration"]["imageScale"] == 300 / 72
    assert provider["configuration"]["modelIdentity"]["files"]["model.safetensors"] == "first-weights"
    assert local_provider["prepared"] == [(model, True)]
    verify_representation_evidence(result.payload, source, derived_resolver=extractor.evidence_resolver(result))
    assert local_provider["calls"] == [model]


def test_model_settings_and_prepared_bytes_change_reuse_identity(tmp_path, local_provider):
    def make(model="ovisocr2", **kwargs):
        return create_docling_ocr_extractor(model, artifacts_path=tmp_path, local_files_only=True, **kwargs)

    original = make().configuration_digest
    assert make().configuration_digest == original
    changed = [
        make("glm-ocr").configuration_digest,
        make("nuextract3").configuration_digest,
        make(max_tokens=8192).configuration_digest,
        make(dpi=200).configuration_digest,
        make(document_timeout=60).configuration_digest,
    ]
    local_provider["weightDigest"] = "changed-weights"
    changed.append(make().configuration_digest)
    assert original not in changed
    assert len(set(changed)) == len(changed)


def test_invalid_model_or_generation_settings_do_not_prepare_weights(tmp_path, local_provider):
    with pytest.raises(ValueError, match="choose a Docling OCR model"):
        create_docling_ocr_extractor("nuextract-json", artifacts_path=tmp_path)
    with pytest.raises(ValueError, match="max_tokens"):
        create_docling_ocr_extractor("ovisocr2", artifacts_path=tmp_path, max_tokens=0)
    assert not local_provider["prepared"]


def test_public_options_import_without_model_libraries():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; from docspec.adapters import DOCLING_OCR_MODELS, create_docling_ocr_extractor; "
                "assert len(DOCLING_OCR_MODELS) == 3; assert callable(create_docling_ocr_extractor); "
                "assert not {'docling', 'mlx', 'mlx_vlm', 'torch', 'transformers'} & sys.modules.keys()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
