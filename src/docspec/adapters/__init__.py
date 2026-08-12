"""Concrete adapters selected only by an application composition root."""

from docspec.adapters.dagster import DagsterAdapterProfile, DagsterDeploymentConfig, build_dagster_definitions
from docspec.adapters.content_fetchers import (
    AnonymousS3ContentFetcher,
    AnonymousS3ContentFetcherConfig,
    HttpsContentFetcher,
    HttpsContentFetcherConfig,
    HttpsContentFetcherError,
    RoutingContentFetcher,
    S3ContentFetcherError,
)
from docspec.adapters.execution import ExternalExecutionBackend, LocalExecutionBackend
from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache, NullProcessorResultCache
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.source_catalog import (
    LocalFileContentFetcher,
    LocalJsonlSourceCatalog,
    LocalSourceReleaseReader,
    SourceReleaseCatalogView,
)
from docspec.adapters.wire_source_release import (
    JsonSchemaWireSourceReleaseGate,
    LocalWireSourceReleaseReader,
    WireReleaseBundlePin,
    WireReleasePins,
    WireSourceReleaseBundle,
    WireSourceReleaseError,
    load_wire_release_pins,
    read_wire_release_bundle,
)
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
    "JsonSchemaWireSourceReleaseGate",
    "HttpsContentFetcher",
    "HttpsContentFetcherConfig",
    "HttpsContentFetcherError",
    "LocalContentAddressedBlobStore",
    "LocalDocumentStoreRepository",
    "LocalFileContentFetcher",
    "LocalJsonControlRepository",
    "LocalJsonlSourceCatalog",
    "LocalJsonlRecordStorage",
    "LocalExecutionBackend",
    "LocalManifestDocumentCatalog",
    "LocalSourceReleaseReader",
    "LocalWireSourceReleaseReader",
    "LocalSqliteProcessorResultCache",
    "LocalSqliteReconciliationWorkspaceFactory",
    "NullProcessorResultCache",
    "RootOnlyBlobProfileStateReachability",
    "SourceReleaseCatalogView",
    "RoutingContentFetcher",
    "S3ContentFetcherError",
    "WireReleaseBundlePin",
    "WireReleasePins",
    "WireSourceReleaseBundle",
    "WireSourceReleaseError",
    "build_dagster_definitions",
    "load_wire_release_pins",
    "read_wire_release_bundle",
]
