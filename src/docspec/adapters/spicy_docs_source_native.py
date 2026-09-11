"""Read source-native artifacts through the installed SpicyDocs reader.

The package stays optional and is loaded only at this adapter boundary. Artifact
producer labels are independent of the reader package: pinned releases from
both accepted producers remain readable through the current SpicyDocs owner.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

from rulespec_artifacts import ArtifactPin, LocalBlobSource, LocalMemberSource, MemberSource

from docspec.errors import DocSpecError, IntegrityError
from docspec.ports.source_catalog import SourceNativeDescription

#: Producer products DocSpec's source-native adapter accepts. A reader module
#: whose SUPPORTED_PRODUCER_PRODUCTS is missing or a strict subset of this set
#: is refused: it would silently hard-refuse releases from the other producer.
ACCEPTED_PRODUCER_PRODUCTS: frozenset[str] = frozenset({"spicy-regs", "spicy-docs"})


class SourceNativeReaderError(RuntimeError, DocSpecError):
    """The optional installed reader is missing or cannot admit the source."""


def _resolve_producer_module(module_name: str) -> ModuleType:
    """Load the current reader, preserving errors from its own dependencies."""
    qualified = f"spicy_docs.{module_name}"
    try:
        return import_module(qualified)
    except ModuleNotFoundError as error:
        if error.name not in ("spicy_docs", qualified):
            raise
        raise SourceNativeReaderError(
            f"the source-native adapter requires an installed spicy-docs package providing {module_name}"
        ) from error


def _require_accepted_reader(module: ModuleType) -> ModuleType:
    """Refuse a reader module that cannot serve every accepted producer product."""

    supported = getattr(module, "SUPPORTED_PRODUCER_PRODUCTS", None)
    if supported is None or not ACCEPTED_PRODUCER_PRODUCTS.issubset(supported):
        raise SourceNativeReaderError(
            f"{module.__name__} does not declare SUPPORTED_PRODUCER_PRODUCTS covering "
            f"{sorted(ACCEPTED_PRODUCER_PRODUCTS)}"
        )
    return module


def spicy_docs_source_profile(name: str) -> object:
    """Resolve one explicit CLI choice without importing a producer package in DocSpec core."""

    module = _resolve_producer_module("source_native_profiles")
    if name == "federal-register":
        return module.FEDERAL_REGISTER_PROFILE
    if name == "regulations-gov-documents":
        return module.REGULATIONS_GOV_DOCUMENT_PROFILE
    if name == "regulations-gov-dockets":
        return module.REGULATIONS_GOV_DOCKET_PROFILE
    if name == "regulations-gov-comments":
        return module.REGULATIONS_GOV_COMMENT_PROFILE
    raise ValueError(f"unsupported source-native profile: {name}")


class SpicyDocsSourceNativeAdapter:
    """Expose source-native rows through DocSpec's structural source port."""

    def __init__(
        self,
        source: MemberSource,
        *,
        blob_source: object,
        profile: object,
        expected_pin: ArtifactPin | None,
        accepted_verifier_implementation_ids: frozenset[str],
    ) -> None:
        module = _require_accepted_reader(_resolve_producer_module("source_native"))
        reader_type = getattr(module, "SourceNativeReleaseReader", None)
        if reader_type is None:
            raise SourceNativeReaderError(f"{module.__name__} has no SourceNativeReleaseReader")
        self._reader = reader_type(
            source,
            blob_source=blob_source,
            profile=profile,
            expected_pin=expected_pin,
            accepted_verifier_implementation_ids=accepted_verifier_implementation_ids,
        )

    @classmethod
    def from_local(
        cls,
        root: Path,
        *,
        blob_root: Path,
        artifact_digest: str,
        profile: object,
        accepted_verifier_implementation_ids: frozenset[str],
        logical_id: str | None = None,
    ) -> SpicyDocsSourceNativeAdapter:
        adapter = cls(
            LocalMemberSource(Path(root)),
            blob_source=LocalBlobSource(Path(blob_root)),
            profile=profile,
            expected_pin=(ArtifactPin(logical_id, artifact_digest) if logical_id is not None else None),
            accepted_verifier_implementation_ids=accepted_verifier_implementation_ids,
        )
        if adapter._reader.pin.artifact_digest != artifact_digest:
            raise IntegrityError("source-native artifact digest differs from the expected digest")
        return adapter

    def describe(self) -> SourceNativeDescription:
        return SourceNativeDescription(
            logical_id=self._reader.pin.logical_id,
            artifact_digest=self._reader.pin.artifact_digest,
            source_system_id=self._reader.source_system_id,
            source_system_version=self._reader.source_system_version,
            source_state_scope=self._reader.source_state_scope,
            source_state_digest=self._reader.source_state_digest,
            source_native_schema_set_digest=self._reader.source_native_schema_set_digest,
        )

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        yield from self._reader.iter_records()

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
        yield from self._reader.iter_renditions()


__all__ = ["SourceNativeReaderError", "SpicyDocsSourceNativeAdapter", "spicy_docs_source_profile"]
