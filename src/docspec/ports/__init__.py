"""DocSpec dependency-inversion interfaces."""

from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.content_fetcher import AcquisitionSource, ContentFetcher, FetchMetadata, FetchStream
from docspec.ports.document_catalog import DocumentCatalog, DocumentCatalogReader
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.execution_backend import ExecutionBackend, SerializedTaskDispatcher, StoreTaskHandler
from docspec.ports.processor_cache import ProcessorResultCache
from docspec.ports.profile_state_reachability import ProfileStateBlobReachability
from docspec.ports.extractor import Extractor
from docspec.ports.processor import Processor
from docspec.ports.record_workspace import RecordWorkspace, RecordWorkspaceFactory
from docspec.ports.record_storage import PartitionPolicy, RecordSchema, RecordStorage
from docspec.ports.reconciliation_workspace import ReconciliationWorkspace, ReconciliationWorkspaceFactory
from docspec.ports.result_sink import ResultSink
from docspec.ports.segmenter import Segmenter
from docspec.ports.source_catalog import (
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    ImmutableSourceCatalogReader,
    SourceInputSelector,
    SourceCatalogMemberSource,
    SourceCatalogPolicy,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogStaging,
    SourceCatalogStore,
    SourceNativeDescription,
    SourceNativeRecordSource,
    SourceNativeRow,
)

__all__ = [
    "BlobStore",
    "CatalogPolicyInputs",
    "CatalogPolicyWorkspace",
    "AcquisitionSource",
    "ContentFetcher",
    "ControlRepository",
    "DocumentCatalog",
    "DocumentCatalogReader",
    "DocumentStoreRepository",
    "ExecutionBackend",
    "ProcessorResultCache",
    "ProfileStateBlobReachability",
    "Extractor",
    "FetchMetadata",
    "FetchStream",
    "PartitionPolicy",
    "Processor",
    "RecordWorkspace",
    "RecordWorkspaceFactory",
    "RecordSchema",
    "RecordStorage",
    "ReconciliationWorkspace",
    "ReconciliationWorkspaceFactory",
    "ResultSink",
    "Segmenter",
    "SerializedTaskDispatcher",
    "SourceCatalogMemberSource",
    "SourceCatalogPolicy",
    "SourceCatalogSnapshot",
    "SourceCatalogSnapshotSummary",
    "SourceCatalogStaging",
    "SourceCatalogStore",
    "SourceInputSelector",
    "SourceNativeDescription",
    "SourceNativeRecordSource",
    "SourceNativeRow",
    "ImmutableSourceCatalogReader",
    "StoreTaskHandler",
]
