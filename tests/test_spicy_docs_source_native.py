"""Admission through the current optional source-native reader."""

from __future__ import annotations

import sys
import types

import pytest

from docspec.adapters import spicy_docs_source_native as adapter_module

_READER_MODULE_NAME = "spicy_docs.source_native"


def _reader(*, products: frozenset[str] | None) -> types.ModuleType:
    module = types.ModuleType(_READER_MODULE_NAME)
    if products is not None:
        module.SUPPORTED_PRODUCER_PRODUCTS = products
    return module


def test_resolves_the_current_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _reader(products=adapter_module.ACCEPTED_PRODUCER_PRODUCTS)
    monkeypatch.setitem(sys.modules, _READER_MODULE_NAME, reader)

    resolved = adapter_module._resolve_producer_module("source_native")

    assert resolved is reader
    assert adapter_module._require_accepted_reader(resolved) is resolved


def test_resolves_the_profiles_module_and_one_named_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = types.ModuleType("spicy_docs.source_native_profiles")
    profiles.FEDERAL_REGISTER_PROFILE = object()
    monkeypatch.setitem(sys.modules, "spicy_docs.source_native_profiles", profiles)

    assert adapter_module.spicy_docs_source_profile("federal-register") is profiles.FEDERAL_REGISTER_PROFILE


@pytest.mark.parametrize("products", [frozenset({"spicy-regs"}), None])
def test_refuses_a_reader_that_cannot_admit_all_pinned_producers(products: frozenset[str] | None) -> None:
    reader = _reader(products=products)

    with pytest.raises(RuntimeError, match=_READER_MODULE_NAME):
        adapter_module._require_accepted_reader(reader)


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
