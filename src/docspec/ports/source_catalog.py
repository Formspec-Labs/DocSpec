"""Build and read immutable source catalogs through DocSpec-owned ports."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, BinaryIO, Protocol

from docspec.domain.identity import require_sha256, require_text
from docspec.domain.references import SourceCatalogRef
from docspec.domain.source_catalog import SourceCatalogItem


@dataclass(frozen=True, slots=True)
class SourceNativeDescription:
    """Immutable identity and completeness facts for one source-native input."""

    logical_id: str
    artifact_digest: str
    source_system_id: str
    source_system_version: str
    source_state_scope: str
    source_state_digest: str
    source_native_schema_set_digest: str

    def __post_init__(self) -> None:
        require_text(self.logical_id, "source-native logical_id")
        require_sha256(self.artifact_digest, "source-native artifact_digest")
        require_text(self.source_system_id, "source_system_id")
        require_text(self.source_system_version, "source_system_version")
        if self.source_state_scope not in {"complete-snapshot", "observed-crawl"}:
            raise ValueError("source_state_scope must be complete-snapshot or observed-crawl")
        require_sha256(self.source_state_digest, "source_state_digest")
        require_sha256(self.source_native_schema_set_digest, "source_native_schema_set_digest")


class SourceNativeRecordSource(Protocol):
    """Stream one already-admitted source-native snapshot without producer types."""

    def describe(self) -> SourceNativeDescription: ...

    def iter_records(self) -> Iterator[Mapping[str, Any]]: ...

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SourceInputSelector:
    """Plain policy data selecting one exact source-native row family."""

    source_system_id: str
    source_system_version: str
    scope_id: str
    schema_name: str
    schema_version: str

    def __post_init__(self) -> None:
        require_text(self.source_system_id, "source input source_system_id")
        require_text(self.source_system_version, "source input source_system_version")
        require_text(self.scope_id, "source input scope_id")
        require_text(self.schema_name, "source input schema_name")
        require_text(self.schema_version, "source input schema_version")

    def to_dict(self) -> dict[str, str]:
        return {
            "sourceSystemId": self.source_system_id,
            "sourceSystemVersion": self.source_system_version,
            "scopeId": self.scope_id,
            "schemaName": self.schema_name,
            "schemaVersion": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> SourceInputSelector:
        if not isinstance(value, Mapping) or set(value) != {
            "sourceSystemId",
            "sourceSystemVersion",
            "scopeId",
            "schemaName",
            "schemaVersion",
        }:
            raise ValueError("source input selector has an invalid closed shape")
        return cls(
            value["sourceSystemId"],
            value["sourceSystemVersion"],
            value["scopeId"],
            value["schemaName"],
            value["schemaVersion"],
        )


@dataclass(frozen=True, slots=True)
class SourceNativeRow:
    """One structurally admitted source record and its matching renditions."""

    description: SourceNativeDescription
    record: Mapping[str, Any]
    renditions: tuple[Mapping[str, Any], ...]


class CatalogPolicyWorkspace(Protocol):
    """Bounded ephemeral exact-key and ordered-row storage for one policy run."""

    def put(
        self,
        namespace: str,
        key: tuple[str, ...],
        value: Mapping[str, Any],
    ) -> None: ...

    def get(self, namespace: str, key: tuple[str, ...]) -> Mapping[str, Any] | None: ...

    def iter_ordered(self, namespace: str) -> Iterator[Mapping[str, Any]]: ...


class CatalogPolicyInputs(Protocol):
    """Builder-owned one-pass access to admitted source-native inputs."""

    @property
    def descriptions(self) -> tuple[SourceNativeDescription, ...]: ...

    def iter_universe_rows(self) -> Iterator[SourceNativeRow]: ...

    def iter_lookup_rows(self, selector: SourceInputSelector) -> Iterator[SourceNativeRow]: ...


class SourceCatalogMemberSource(Protocol):
    """Read artifact members through the shared structural seam."""

    def keys(self) -> Iterable[str]: ...

    def open(self, object_key: str) -> AbstractContextManager[BinaryIO]: ...


class SourceCatalogStaging(SourceCatalogMemberSource, Protocol):
    """One unpublished write transaction that is also a readable member source."""

    def write(self, object_key: str, chunks: Iterable[bytes]) -> None: ...

    def commit(self, reference: SourceCatalogRef) -> SourceCatalogRef: ...


class SourceCatalogStore(Protocol):
    """Create an isolated source-catalog transaction and resolve published roots."""

    def stage(self) -> AbstractContextManager[SourceCatalogStaging]: ...

    def source_for(self, reference: SourceCatalogRef) -> SourceCatalogMemberSource: ...


class SourceCatalogPolicy(Protocol):
    """Apply one DocSpec-owned interpretation policy to neutral source rows."""

    @property
    def policy_id(self) -> str: ...

    @property
    def policy_version(self) -> str: ...

    @property
    def configuration(self) -> Mapping[str, Any]: ...

    @property
    def universe_input(self) -> SourceInputSelector: ...

    def iter_items(
        self,
        inputs: CatalogPolicyInputs,
        workspace: CatalogPolicyWorkspace,
    ) -> Iterator[SourceCatalogItem]: ...


@dataclass(frozen=True, slots=True)
class SourceCatalogSnapshotSummary:
    """Identity and completeness evidence exposed to downstream consumers."""

    logical_id: str
    artifact_digest: str
    catalog_id: str
    catalog_state_digest: str
    requested_universe_set_digest: str
    selected_source_set_digest: str
    item_count: int
    disposition_counts: Mapping[str, int]
    partitions: tuple[str, ...]
    item_member_path: str

    def __post_init__(self) -> None:
        require_text(self.logical_id, "source catalog logical_id")
        require_sha256(self.artifact_digest, "source catalog artifact_digest")
        require_text(self.catalog_id, "catalog_id")
        require_sha256(self.catalog_state_digest, "catalog_state_digest")
        require_sha256(self.requested_universe_set_digest, "requested_universe_set_digest")
        require_sha256(self.selected_source_set_digest, "selected_source_set_digest")
        require_text(self.item_member_path, "source catalog item_member_path")
        if self.item_count < 0 or any(value < 0 for value in self.disposition_counts.values()):
            raise ValueError("source catalog counts must be non-negative")
        if sum(self.disposition_counts.values()) != self.item_count:
            raise ValueError("source catalog disposition counts must account for every row")
        object.__setattr__(
            self,
            "disposition_counts",
            MappingProxyType(dict(self.disposition_counts)),
        )


@dataclass(slots=True)
class SourceCatalogSnapshot:
    """One verified root and its bounded full normative row stream."""

    summary: SourceCatalogSnapshotSummary
    items: Iterator[SourceCatalogItem]


class ImmutableSourceCatalogReader(Protocol):
    """Open one complete immutable DocSpec source-catalog snapshot."""

    def open_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshot: ...
