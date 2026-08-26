"""Public build and read surface for DocSpec-owned immutable source catalogs."""

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
    RegulationsGovSamplePolicy,
)
from docspec.adapters.source_catalog_artifact import (
    SourceCatalogArtifactReader,
    SourceCatalogBuildRequest,
    SourceCatalogBuildResult,
    SourceCatalogBuilder,
    requested_universe_set_digest,
    selected_source_set_digest,
    source_catalog_producer,
)
from docspec.domain.source_catalog import (
    CatalogDisposition,
    SourceCatalogCandidate,
    SourceCatalogItem,
    SourceCatalogSelection,
)
from docspec.ports.source_catalog import (
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    ImmutableSourceCatalogReader,
    SourceInputSelector,
    SourceCatalogPolicy,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogStore,
    SourceNativeDescription,
    SourceNativeRecordSource,
    SourceNativeRow,
)

__all__ = [
    "CatalogPolicyInputs",
    "CatalogPolicyWorkspace",
    "CatalogDisposition",
    "FederalRegisterCatalogPolicy",
    "ImmutableSourceCatalogReader",
    "LocalSourceCatalogStore",
    "RegulationsGovCatalogPolicy",
    "RegulationsGovSamplePolicy",
    "SqliteCatalogPolicyWorkspace",
    "SourceCatalogArtifactReader",
    "SourceCatalogBuildRequest",
    "SourceCatalogBuildResult",
    "SourceCatalogBuilder",
    "SourceCatalogCandidate",
    "SourceCatalogItem",
    "SourceCatalogPolicy",
    "SourceCatalogSelection",
    "SourceCatalogSnapshot",
    "SourceCatalogSnapshotSummary",
    "SourceCatalogStore",
    "SourceInputSelector",
    "SourceNativeDescription",
    "SourceNativeRecordSource",
    "SourceNativeRow",
    "requested_universe_set_digest",
    "selected_source_set_digest",
    "source_catalog_producer",
]
