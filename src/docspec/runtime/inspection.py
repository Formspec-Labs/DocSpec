"""Open existing dataset evidence without constructing execution services."""

from __future__ import annotations

from pathlib import Path
from tempfile import gettempdir
from types import TracebackType
from typing import Self

from rulespec_artifacts import Producer

from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.adapters.storage import LocalParquetRecordStorage
from docspec.application.inspection import InspectionView
from docspec.domain.plans import ProcessingPlan
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import ArtifactRef, DocumentReleaseRef
from docspec.runtime.storage import _local_profiles, _local_storage, _profile_limit
from docspec.workspace import LocalWorkspace


class LocalInspectionView(InspectionView):
    """An inspection whose local native query resources belong to the caller."""

    _records: LocalParquetRecordStorage

    def close(self) -> None:
        """Release native resources after active iterators have stopped."""
        self._records.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_local_inspection(
    plan: ProcessingPlan,
    workspace: LocalWorkspace,
    *,
    document_release_producer: Producer,
    run_ref: ArtifactRef | None = None,
    release_ref: DocumentReleaseRef | None = None,
    source_catalog_producer: Producer | None = None,
) -> LocalInspectionView:
    """Inspect a plan's saved progress, an exact run, or its retained result.

    Supply at most one result reference. Omitting both observes current saved
    progress, which is not an immutable attempt. Existing storage and the plan's
    pinned profiles are required; this operation creates no dataset state.
    Comparisons use disposable, bounded SQLite scratch in the system temporary
    directory. Use as a context manager or close after its iterators stop.
    No fetcher, processor, stage implementation, or deadline is needed.

    Source coverage is unavailable unless its producer is explicitly accepted
    with ``source_catalog_producer``. Document acceptance is a separate choice.
    """
    if run_ref is not None and release_ref is not None:
        raise ValueError("choose a run reference or a retained release reference")
    profiles = _local_profiles(plan, workspace)
    controls, stores, records, blobs, catalog = _local_storage(
        workspace.roots, profiles, document_release_producer, create=False,
    )
    try:
        source_summary = None
        if source_catalog_producer is not None:
            source_summary = SourceCatalogArtifactReader(
                LocalSourceCatalogStore(workspace.roots["sourceCatalog"], create=False),
                producer=source_catalog_producer,
            ).admit_snapshot(plan.source_catalog).summary
        record_profile = profiles[ProfileRole.RECORD_STORAGE]
        return LocalInspectionView(
            plan, controls=controls, stores=stores, records=records, blobs=blobs,
            catalog=catalog,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                Path(gettempdir()).resolve(),
                max_spooled_bytes=_profile_limit(record_profile, "maxMergeScratchBytes"),
                max_record_bytes=_profile_limit(record_profile, "maxRecordBytes"),
                read_batch_size=1,
            ),
            run_ref=run_ref, release_ref=release_ref, source_summary=source_summary,
        )
    except BaseException:
        records.close()
        raise
