"""Concrete adapters selected only by an application composition root."""

from docspec.adapters.dagster import DagsterAdapterProfile, DagsterDeploymentConfig, build_dagster_definitions
from docspec.adapters.execution import ExternalExecutionBackend, LocalExecutionBackend
from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache, NullProcessorResultCache
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.source_catalog import LocalFileContentFetcher, LocalJsonlSourceCatalog
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
    RootOnlyBlobProfileStateReachability,
)

__all__ = [
    "ExternalExecutionBackend",
    "DagsterAdapterProfile",
    "DagsterDeploymentConfig",
    "LocalContentAddressedBlobStore",
    "LocalDocumentStoreRepository",
    "LocalFileContentFetcher",
    "LocalJsonControlRepository",
    "LocalJsonlSourceCatalog",
    "LocalJsonlRecordStorage",
    "LocalExecutionBackend",
    "LocalManifestDocumentCatalog",
    "LocalSqliteProcessorResultCache",
    "LocalSqliteReconciliationWorkspaceFactory",
    "NullProcessorResultCache",
    "RootOnlyBlobProfileStateReachability",
    "build_dagster_definitions",
]
