"""Optional outer adapter for the installed SpicyRegs source-native reader."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from importlib import import_module
from pathlib import Path
from typing import Any

from rulespec_artifacts import ArtifactPin, LocalMemberSource, MemberSource

from docspec.errors import IntegrityError
from docspec.ports.source_catalog import SourceNativeDescription


def spicyregs_source_profile(name: str) -> object:
    """Resolve one explicit CLI choice without importing SpicyRegs in DocSpec core."""

    try:
        module = import_module("spicy_regs.source_native_profiles")
    except ModuleNotFoundError as error:
        raise RuntimeError("the SpicyRegs source-native adapter requires an installed spicy-regs package") from error
    if name == "federal-register":
        return module.FEDERAL_REGISTER_PROFILE
    if name == "regulations-gov-documents":
        return module.REGULATIONS_GOV_DOCUMENT_PROFILE
    if name == "regulations-gov-dockets":
        return module.REGULATIONS_GOV_DOCKET_PROFILE
    raise ValueError(f"unsupported SpicyRegs source profile: {name}")


class SpicyRegsSourceNativeAdapter:
    """Expose SpicyRegs rows through DocSpec's structural source port."""

    def __init__(
        self,
        source: MemberSource,
        *,
        profile: object,
        expected_pin: ArtifactPin | None,
        accepted_verifier_implementation_ids: frozenset[str],
    ) -> None:
        try:
            module = import_module("spicy_regs.source_native")
        except ModuleNotFoundError as error:
            raise RuntimeError("the SpicyRegs source-native adapter requires an installed spicy-regs package") from error
        reader_type = getattr(module, "SourceNativeReleaseReader", None)
        if reader_type is None:
            raise RuntimeError("the installed SpicyRegs package has no SourceNativeReleaseReader")
        self._reader = reader_type(
            source,
            profile=profile,
            expected_pin=expected_pin,
            accepted_verifier_implementation_ids=accepted_verifier_implementation_ids,
        )

    @classmethod
    def from_local(
        cls,
        root: Path,
        *,
        artifact_digest: str,
        profile: object,
        accepted_verifier_implementation_ids: frozenset[str],
        logical_id: str | None = None,
    ) -> SpicyRegsSourceNativeAdapter:
        adapter = cls(
            LocalMemberSource(Path(root)),
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


__all__ = ["SpicyRegsSourceNativeAdapter", "spicyregs_source_profile"]
