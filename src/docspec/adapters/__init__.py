"""Concrete adapters selected only by an application composition root.

Adapter modules load on first attribute access so optional scheduler and
network dependencies stay out of metadata-only import paths.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AnonymousS3ContentFetcher": "docspec.adapters.content_fetchers",
    "AnonymousS3ContentFetcherConfig": "docspec.adapters.content_fetchers",
    "DagsterRuntime": "docspec.adapters.dagster",
    "ExternalExecutionBackend": "docspec.adapters.execution",
    "HttpsContentFetcher": "docspec.adapters.content_fetchers",
    "HttpsContentFetcherConfig": "docspec.adapters.content_fetchers",
    "HttpsContentFetcherError": "docspec.adapters.content_fetchers",
    "LocalContentAddressedBlobStore": "docspec.adapters.storage",
    "LocalDocumentStoreRepository": "docspec.adapters.storage",
    "LocalExecutionBackend": "docspec.adapters.execution",
    "LocalFileContentFetcher": "docspec.adapters.content_fetchers",
    "LocalJsonControlRepository": "docspec.adapters.storage",
    "LocalJsonlRecordStorage": "docspec.adapters.storage",
    "LocalManifestDocumentCatalog": "docspec.adapters.storage",
    "LocalSourceCatalogCurrentPointer": "docspec.adapters.source_catalog_store",
    "LocalSourceCatalogStore": "docspec.adapters.source_catalog_store",
    "LocalSqliteProcessorResultCache": "docspec.adapters.processor_cache",
    "LocalSqliteReconciliationWorkspaceFactory": "docspec.adapters.reconciliation",
    "NullProcessorResultCache": "docspec.adapters.processor_cache",
    "RootOnlyBlobProfileStateReachability": "docspec.adapters.storage",
    "RoutingContentFetcher": "docspec.adapters.content_fetchers",
    "S3ContentFetcherError": "docspec.adapters.content_fetchers",
    "SqliteCatalogPolicyWorkspace": "docspec.adapters.catalog_policy_workspace",
    "SpicyRegsSourceNativeAdapter": "docspec.adapters.spicyregs_source_native",
    "TiktokenCounter": "docspec.adapters.token_counters",
    "build_dagster_definitions": "docspec.adapters.dagster",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
