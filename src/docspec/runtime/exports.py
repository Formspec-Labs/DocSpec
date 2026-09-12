"""Read-only workspace composition for publishing an existing retained result."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Literal

from rulespec_artifacts import ArtifactPin, Producer

from docspec.adapters.result_export.io import ADMISSIONS, require_limit
from docspec.adapters.result_export.writer import export_result
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import DocumentReleaseRef
from docspec.errors import IntegrityError
from docspec.runtime.storage import _local_profiles, _local_storage
from docspec.workspace import LocalWorkspace


def export_local_result(
    plan: ProcessingPlan, workspace: LocalWorkspace, release_ref: DocumentReleaseRef,
    destination: Path, *, admission: Literal["retained-evidence", "nonempty-text"],
    document_release_producer: Producer, export_producer: Producer, max_output_bytes: int,
) -> ArtifactPin:
    """Export the entire retained active result without running any stages.

    ``retained-evidence`` preserves accounted failures and capture-only results.
    ``nonempty-text`` requires nonempty UTF-8 text for each selected document and
    refuses terminal failures. Both preserve every row; neither claims semantic
    completeness. Producer acceptance and the total artifact byte bound are
    explicit. Publication replaces no existing different destination.

    The output contains active records, exact captured/representation/segment
    bytes and their typed stage/processor evidence. It is independently readable
    through ``docspec.result_export.open_result_export``. Historical runs,
    source catalogs and provider resources remain external provenance pins;
    this is a consumer dataset, not an executable workspace backup.
    """
    require_limit(max_output_bytes)
    if admission not in ADMISSIONS:
        raise ValueError(f"admission must be one of {ADMISSIONS}")
    profiles = _local_profiles(plan, workspace)
    controls, _, records, blobs, catalog = _local_storage(
        workspace.roots, profiles, document_release_producer, create=False,
    )
    with closing(records):
        release = catalog.audit(release_ref)
        if release.processing_plan.artifact_id != plan.plan_id:
            raise IntegrityError("export retained result belongs to another processing plan")
        reader = catalog.open_reader(release_ref)
        return export_result(reader, controls, blobs, release_ref, destination,
            admission=admission, producer=export_producer, max_output_bytes=max_output_bytes)
