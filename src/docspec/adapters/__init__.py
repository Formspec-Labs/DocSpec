"""Concrete adapters selected only by an application composition root."""

from docspec.adapters.dagster import DagsterAdapterProfile, DagsterDeploymentConfig, build_dagster_definitions
from docspec.adapters.content_fetchers import (
    AnonymousS3ContentFetcher,
    AnonymousS3ContentFetcherConfig,
    HttpsContentFetcher,
    HttpsContentFetcherConfig,
    HttpsContentFetcherError,
    LocalFileContentFetcher,
    RoutingContentFetcher,
    S3ContentFetcherError,
)
from docspec.adapters.execution import ExternalExecutionBackend, LocalExecutionBackend
from docspec.adapters.platform_artifact import LocalPlatformSourceCatalog, PlatformSourceCatalog
from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache, NullProcessorResultCache
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
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
    "AnonymousS3ContentFetcher",
    "AnonymousS3ContentFetcherConfig",
    "DagsterAdapterProfile",
    "DagsterDeploymentConfig",
    "HttpsContentFetcher",
    "HttpsContentFetcherConfig",
    "HttpsContentFetcherError",
    "LocalContentAddressedBlobStore",
    "LocalDocumentStoreRepository",
    "LocalFileContentFetcher",
    "LocalJsonControlRepository",
    "LocalJsonlRecordStorage",
    "LocalExecutionBackend",
    "LocalManifestDocumentCatalog",
    "LocalPlatformSourceCatalog",
    "LocalSqliteProcessorResultCache",
    "LocalSqliteReconciliationWorkspaceFactory",
    "NullProcessorResultCache",
    "PlatformSourceCatalog",
    "RootOnlyBlobProfileStateReachability",
    "RoutingContentFetcher",
    "S3ContentFetcherError",
    "build_dagster_definitions",
]
