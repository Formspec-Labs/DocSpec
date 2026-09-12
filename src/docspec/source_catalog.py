"""Public build and read surface for DocSpec-owned immutable source catalogs."""

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import (
    LocalSourceCatalogCurrentPointer,
    LocalSourceCatalogStore,
)
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.supplied_records_catalog import SuppliedRecordCatalogPolicy
from docspec.adapters.supplied_records import SuppliedRecordSource
from docspec.adapters.spicy_docs_source_native import SpicyDocsSourceNativeAdapter, spicy_docs_source_profile
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
    RegulationsGovSamplePolicy,
)
from docspec.adapters.catalog_artifact.reader import (
    AdmittedSourceCatalog,
    SourceCatalogArtifactReader,
    open_admitted_source_catalog,
)
from docspec.adapters.catalog_artifact.builder import (
    SourceCatalogBuildRequest,
    SourceCatalogBuildResult,
    SourceCatalogBuilder,
)
from docspec.adapters.catalog_artifact.digests import (
    requested_universe_set_digest,
    selected_source_set_digest,
)
from docspec.adapters.catalog_artifact.rules import (
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
    LocatedSourceCatalogItem,
    LocatedSourceCatalogMapping,
    SourceInputSelector,
    SourceCatalogPolicy,
    SourceCatalogCurrentPointer,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogSuccession,
    SourceCatalogStore,
    SourceNativeDescription,
    SourceNativeRecordSource,
    SourceNativeRow,
)

__all__ = [
    "AdmittedSourceCatalog",
    "CatalogPolicyInputs",
    "CatalogPolicyWorkspace",
    "CatalogDisposition",
    "FederalRegisterCatalogPolicy",
    "ImmutableSourceCatalogReader",
    "LocatedSourceCatalogItem",
    "LocatedSourceCatalogMapping",
    "LocalSourceCatalogStore",
    "LocalSourceCatalogCurrentPointer",
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
    "SourceCatalogCurrentPointer",
    "SourceCatalogSelection",
    "SourceCatalogSnapshot",
    "SourceCatalogSnapshotSummary",
    "SourceCatalogSuccession",
    "SourceCatalogStore",
    "SourceInputSelector",
    "SourceNativeDescription",
    "SourceNativeRecordSource",
    "SourceNativeRow",
    "SpicyDocsSourceNativeAdapter",
    "SuppliedRecordCatalogPolicy",
    "SuppliedRecordSource",
    "requested_universe_set_digest",
    "selected_source_set_digest",
    "source_catalog_producer",
    "open_admitted_source_catalog",
    "spicy_docs_source_profile",
]
