"""Small immutable references passed between workers and storage profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from docspec.domain.identity import require_sha256, require_text




@dataclass(frozen=True, slots=True)
class BlobRef:
    locator: str
    digest: str
    byte_size: int
    media_type: str

    def __post_init__(self) -> None:
        require_text(self.locator, "blob locator")
        require_sha256(self.digest, "blob digest")
        require_text(self.media_type, "blob media_type")
        if self.byte_size < 0:
            raise ValueError("blob byte_size must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "digest": self.digest,
            "byteSize": self.byte_size,
            "mediaType": self.media_type,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        if set(value) != {"locator", "digest", "byteSize", "mediaType"}:
            raise ValueError("blob reference has an invalid closed shape")
        return cls(value["locator"], value["digest"], value["byteSize"], value["mediaType"])




@dataclass(frozen=True, slots=True)
class LayerRef:
    layer_id: str
    layer_kind: str
    schema_id: str
    profile_id: str
    state_ref: str
    digest: str
    record_count: int

    def __post_init__(self) -> None:
        for label, value in (
            ("layer_id", self.layer_id),
            ("layer_kind", self.layer_kind),
            ("schema_id", self.schema_id),
            ("profile_id", self.profile_id),
            ("state_ref", self.state_ref),
        ):
            require_text(value, label)
        require_sha256(self.digest, "layer digest")
        if self.record_count < 0:
            raise ValueError("layer record_count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "layerId": self.layer_id,
            "layerKind": self.layer_kind,
            "schemaId": self.schema_id,
            "profileId": self.profile_id,
            "stateRef": self.state_ref,
            "digest": self.digest,
            "recordCount": self.record_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {"layerId", "layerKind", "schemaId", "profileId", "stateRef", "digest", "recordCount"}
        if set(value) != expected:
            raise ValueError("layer reference has an invalid closed shape")
        return cls(
            value["layerId"],
            value["layerKind"],
            value["schemaId"],
            value["profileId"],
            value["stateRef"],
            value["digest"],
            value["recordCount"],
        )




@dataclass(frozen=True, slots=True)
class SourceCatalogRef:
    catalog_id: str
    locator: str
    digest: str

    def __post_init__(self) -> None:
        require_text(self.catalog_id, "catalog_id")
        require_text(self.locator, "catalog locator")
        require_sha256(self.digest, "catalog digest")

    def to_dict(self) -> dict[str, str]:
        return {"catalogId": self.catalog_id, "locator": self.locator, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        if set(value) != {"catalogId", "locator", "digest"}:
            raise ValueError("source catalog reference has an invalid closed shape")
        return cls(value["catalogId"], value["locator"], value["digest"])
