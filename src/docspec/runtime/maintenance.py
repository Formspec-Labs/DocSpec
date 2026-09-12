"""Compose existing retention services without constructing execution plugins."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from rulespec_artifacts import Producer

from docspec.adapters.blob_inventory import preview_blob_inventory
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import RootOnlyBlobProfileStateReachability
from docspec.application.maintenance import BlobRetentionSetService
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, StoreRef
from docspec.domain.storage import PartitionPolicy
from docspec.runtime.storage import _local_profiles, _local_storage, _put_blob_profile_state
from docspec.workspace import LocalWorkspace


def build_local_retention_set(
    plan: ProcessingPlan,
    workspace: LocalWorkspace,
    *,
    document_release_producer: Producer,
    max_spooled_bytes: int,
    retained_releases: Iterable[DocumentReleaseRef] = (),
    retained_stores: Iterable[StoreRef] = (),
    retained_plans: Iterable[ArtifactRef] = (),
) -> ArtifactRef:
    """Save verified blob dependencies of explicit retained results/checkpoints.

    Include every result and checkpoint to keep. Standalone stores require their
    saved processing-plan references (available on the prepared handoff).
    Release roots supply their plans and required predecessors automatically.
    The plan argument selects local storage profiles; it does not select roots.
    Only a bounded scratch index and immutable retention evidence are written.
    """
    profiles = _local_profiles(plan, workspace)
    controls, stores, records, blobs, catalog = _local_storage(
        workspace.roots, profiles, document_release_producer, create=False,
    )
    with closing(records):
        factory = LocalSqliteReconciliationWorkspaceFactory(
            Path(gettempdir()).resolve(), max_spooled_bytes=max_spooled_bytes,
        )
        return BlobRetentionSetService(
            controls=controls, stores=stores, records=records, blobs=blobs, document_catalog=catalog,
            profile_state_reachability=RootOnlyBlobProfileStateReachability(), workspace_factory=factory,
            partition_policy=PartitionPolicy("source-item-sha256-v1", plan.partition_count),
        ).build(
            blob_profile_state=_put_blob_profile_state(controls, plan, blobs),
            retained_releases=retained_releases, retained_stores=retained_stores, retained_plans=retained_plans,
        )


def preview_local_blob_inventory(
    plan: ProcessingPlan,
    workspace: LocalWorkspace,
    retention_reference: ArtifactRef,
    *,
    document_release_producer: Producer,
    max_spooled_bytes: int,
    minimum_age_seconds: int = 0,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Inspect local objects relative to a saved retention set, without writes.

    First build a fresh set from all required immutable roots. This reader checks
    the supplied set and each listed blob; it does not independently rederive
    the completeness of an imported set or discover other retained attempts.
    Candidates mean unreferenced by that set at inspection time, never safe
    deletion authority. Scratch lives outside the dataset and is removed on exit.
    """
    profiles = _local_profiles(plan, workspace)
    controls, _, records, blobs, _ = _local_storage(
        workspace.roots, profiles, document_release_producer, create=False,
    )
    with closing(records):
        return preview_blob_inventory(
            retention_reference, plan=plan, controls=controls, records=records, blobs=blobs,
            index_root=Path(gettempdir()).resolve(), max_index_bytes=max_spooled_bytes,
            minimum_age_seconds=minimum_age_seconds, sample_limit=sample_limit,
        )
