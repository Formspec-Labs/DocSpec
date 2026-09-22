"""Source-native adapter admission: the current optional spicy-docs reader is resolved and must declare the
'spicy-docs' producer product, and a missing or broken reader refuses as a structured CLI failure before any
catalog publication.

Covers resolving the profiles module and one named profile, refusing a reader with another producer product,
not trying a predecessor module on failure, preserving a transitive import error, requiring the public
collection-outcome API, and explicit CLI acceptance of partial-rejection inputs being recorded.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from docspec.adapters import spicy_docs_source_native as adapter_module
from docspec.entrypoint import main
from tests.support.source_catalog_cli import source_catalog_build_arguments

_READER_MODULE_NAME = "spicy_docs.source_native"


def _reader(*, product: str | None) -> types.ModuleType:
    """Build a fake reader module with an optional CURRENT_PRODUCER_PRODUCT."""
    module = types.ModuleType(_READER_MODULE_NAME)
    if product is not None:
        module.CURRENT_PRODUCER_PRODUCT = product
    return module


def test_resolves_the_current_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _reader(product="spicy-docs")
    monkeypatch.setitem(sys.modules, _READER_MODULE_NAME, reader)

    resolved = adapter_module._resolve_producer_module("source_native")

    assert resolved is reader
    assert adapter_module._require_current_reader(resolved) is resolved


def test_resolves_the_profiles_module_and_one_named_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = types.ModuleType("spicy_docs.source_native.profiles")
    profiles.FEDERAL_REGISTER_PROFILE = object()
    monkeypatch.setitem(sys.modules, "spicy_docs.source_native.profiles", profiles)

    assert adapter_module.spicy_docs_source_profile("federal-register") is profiles.FEDERAL_REGISTER_PROFILE


@pytest.mark.parametrize("product", ["spicy-regs", "another-producer", None])
def test_refuses_a_reader_without_the_selected_current_producer(product: str | None) -> None:
    reader = _reader(product=product)

    with pytest.raises(adapter_module.SourceNativeReaderError, match="CURRENT_PRODUCER_PRODUCT 'spicy-docs'"):
        adapter_module._require_current_reader(reader)


@pytest.mark.parametrize("missing", ["spicy_docs", _READER_MODULE_NAME])
def test_missing_current_reader_does_not_try_a_predecessor(
    monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    attempted = []

    def import_module(name: str) -> types.ModuleType:
        attempted.append(name)
        raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)

    monkeypatch.setattr(adapter_module, "import_module", import_module)

    with pytest.raises(RuntimeError, match="requires an installed spicy-docs package"):
        adapter_module._resolve_producer_module("source_native")

    assert attempted == [_READER_MODULE_NAME]


def test_a_broken_reader_preserves_its_transitive_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = ModuleNotFoundError("No module named 'polars'", name="polars")

    def import_module(name: str) -> types.ModuleType:
        assert name == _READER_MODULE_NAME
        raise failure

    monkeypatch.setattr(adapter_module, "import_module", import_module)

    with pytest.raises(ModuleNotFoundError) as caught:
        adapter_module._resolve_producer_module("source_native")

    assert caught.value is failure


def test_missing_producer_is_a_structured_cli_failure_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "catalog"
    arguments = source_catalog_build_arguments(
        tmp_path, destination=destination,
    )
    attempted: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        attempted.append(name)
        raise ModuleNotFoundError("No module named 'spicy_docs'", name="spicy_docs")

    monkeypatch.setattr(adapter_module, "import_module", import_module)

    assert main(arguments) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "format": "docspec-cli-error",
        "formatVersion": "1.0",
        "errorType": "SourceNativeReaderError",
        "message": (
            "the source-native adapter requires an installed spicy-docs package "
            "providing source_native.profiles"
        ),
        "verdict": "fail",
    }
    assert attempted == ["spicy_docs.source_native.profiles"]
    assert not destination.exists()


def test_reader_without_public_outcomes_is_refused_before_catalog_publication(tmp_path, monkeypatch, capsys):
    from tests.support.source_catalog_cli import install_fake_source_native

    install_fake_source_native(monkeypatch)
    reader = sys.modules[_READER_MODULE_NAME].SourceNativeReleaseReader
    original_init = reader.__init__

    def initialize_without_outcomes(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        del self.collection_outcome

    monkeypatch.setattr(reader, "__init__", initialize_without_outcomes)
    destination = tmp_path / "catalog"
    assert main(source_catalog_build_arguments(tmp_path, destination=destination)) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["errorType"] == "SourceNativeReaderError"
    assert "required public collection outcome API" in failure["message"]
    assert not destination.exists()


def test_cli_requires_and_records_explicit_partial_input_acceptance(tmp_path, monkeypatch, capsys):
    from tests.support.source_catalog_cli import install_fake_source_native

    install_fake_source_native(monkeypatch)
    reader = sys.modules[_READER_MODULE_NAME].SourceNativeReleaseReader
    original_init = reader.__init__

    def initialize_partial(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.collection_outcome.update(recordOutcome="partial-rejection", failedRecordCount=1)

    monkeypatch.setattr(reader, "__init__", initialize_partial)
    destination = tmp_path / "catalog"
    arguments = source_catalog_build_arguments(tmp_path, destination=destination)
    assert main(arguments) == 2
    assert "partial-rejection" in capsys.readouterr().err
    assert not destination.exists()
    assert main(arguments + ["--accepted-record-outcome", "partial-rejection"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["acceptedRecordOutcomes"] == ["partial-rejection"]
    assert saved["sourceNativeInputs"][0]["collectionOutcome"]["failedRecordCount"] == 1
