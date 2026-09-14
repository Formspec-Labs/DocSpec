"""Build or read a catalog alone using the workspace's existing source storage."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer, Supersedes

from docspec.adapters.catalog_artifact.builder import (
    SourceCatalogBuilder, SourceCatalogBuildRequest, SourceCatalogBuildResult, _snapshot_sources,
)
from docspec.adapters.catalog_artifact.reader import AdmittedSourceCatalog, SourceCatalogArtifactReader
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.catalog_preview import preview_catalog, validate_preview_limits
from docspec.domain.references import SourceCatalogRef
from docspec.domain.source_outcomes import DEFAULT_ACCEPTED_RECORD_OUTCOMES
from docspec.ports.source_catalog import SourceCatalogPolicy, SourceNativeRecordSource
from docspec.runtime.core import CoreWorkspace


def _catalog_root(workspace):
    return (workspace.path if isinstance(workspace, CoreWorkspace) else Path(workspace).resolve()) / "sourceCatalog"


def build_local_catalog(
    sources: Sequence[SourceNativeRecordSource],
    workspace: CoreWorkspace | Path,
    *,
    policy: SourceCatalogPolicy,
    catalog_id: str,
    producer: Producer,
    max_scratch_bytes: int,
    supersedes: Supersedes | None = None,
    resume_workspace: Path | None = None,
    accepted_record_outcomes: frozenset[str] = DEFAULT_ACCEPTED_RECORD_OUTCOMES,
) -> SourceCatalogBuildResult:
    """Build through the ordinary catalog gate without assembling local adapters.

    Only source-catalog storage is created. A supplied resume path retains the
    builder's existing recovery state; otherwise scratch is removed on exit.
    The scratch bound reserves SQLite database and journal space, separately
    from catalog artifact bytes. No current pointer or processing run changes.
    Reported record rejections require explicit acceptance; partial and total
    rejection are distinct choices. An unreported outcome makes no collection
    success claim. Every accepted input still contributes to a full snapshot.
    """

    if not isinstance(producer, Producer):
        raise TypeError("catalog output producer must be supplied explicitly")
    Producer.from_dict(producer.as_dict(), path="catalog output producer")
    if not sources:
        raise ValueError("a source catalog requires at least one source-native input")
    request = SourceCatalogBuildRequest(catalog_id, producer, supersedes, accepted_record_outcomes)
    sources = _snapshot_sources(sources, request.accepted_record_outcomes)
    if resume_workspace is not None:
        resume_workspace = Path(resume_workspace)
        if not resume_workspace.is_absolute():
            raise ValueError("catalog resume workspace must be an absolute path")
    with SqliteCatalogPolicyWorkspace(path=resume_workspace, max_scratch_bytes=max_scratch_bytes) as scratch:
        return SourceCatalogBuilder(
            store=LocalSourceCatalogStore(_catalog_root(workspace)),
            policy=policy, request=request, workspace_factory=lambda: nullcontext(scratch),
        ).build(sources)


def open_local_catalog(
    reference: SourceCatalogRef,
    workspace: CoreWorkspace | Path,
    *,
    producer: Producer,
) -> AdmittedSourceCatalog:
    """Admit an exact catalog with explicit acceptance, creating no directories.

    The returned existing reader provides summary and repeatable bounded row
    streams. Exhaust or close each stream, as with any admitted source catalog.
    """

    if not isinstance(producer, Producer):
        raise TypeError("catalog producer acceptance must be supplied explicitly")
    return SourceCatalogArtifactReader(
        LocalSourceCatalogStore(_catalog_root(workspace), create=False), producer=producer,
    ).admit_snapshot(reference)


def preview_local_catalog(
    reference: SourceCatalogRef,
    workspace: CoreWorkspace | Path,
    *,
    producer: Producer,
    previous_ref: SourceCatalogRef | None = None,
    sample_limit: int = 20,
    max_sample_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Explain selected candidates, exclusions, and changes before acquisition.

    Compare two exact catalogs from this workspace with the same explicit
    producer acceptance. Counts stream the complete population; current-row
    and change samples each cap their item count and canonical entry bytes.
    Zero limits keep complete counts without those samples. No directories,
    pointers, processing tasks, or document fetches are created.

    Successive catalogs are full snapshots. Omission is reported explicitly;
    input scope such as observed-crawl does not request append semantics.
    """

    validate_preview_limits(sample_limit, max_sample_bytes)
    current = open_local_catalog(reference, workspace, producer=producer)
    previous = None if previous_ref is None else open_local_catalog(previous_ref, workspace, producer=producer)
    return preview_catalog(
        current.summary, current.iter_mappings(),
        previous_summary=None if previous is None else previous.summary,
        previous_rows=iter(()) if previous is None else previous.iter_mappings(),
        sample_limit=sample_limit, max_sample_bytes=max_sample_bytes,
    )
