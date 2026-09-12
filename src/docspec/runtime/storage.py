"""Local storage assembly using admitted machine-profile limits."""

from __future__ import annotations

from pathlib import Path

from rulespec_artifacts import Producer

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore, LocalDocumentStoreRepository, LocalJsonControlRepository,
    LocalParquetRecordStorage, LocalManifestDocumentCatalog,
)
from docspec.domain.plans import ProcessingPlan
from docspec.domain.identity import stable_urn
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import ArtifactRef
from docspec.errors import ProfileError
from docspec.profile_registry import ProfileRegistry, RegisteredProfile
from docspec.workspace import LocalWorkspace

_LOCAL_PROFILE_SET_ID = "urn:docspec:profile-set:portable-local:1"


_LOCAL_PROFILE_MODULES = {
    ProfileRole.RELEASE_MANIFEST: "docspec.domain.release:DocumentRelease",
    ProfileRole.DOCUMENT_CATALOG: "docspec.adapters.storage:LocalManifestDocumentCatalog",
    ProfileRole.RECORD_STORAGE: "docspec.adapters.storage:LocalParquetRecordStorage",
    ProfileRole.BLOB_STORAGE: "docspec.adapters.storage:LocalContentAddressedBlobStore",
    ProfileRole.DOCUMENT_STORE: "docspec.adapters.storage:LocalDocumentStoreRepository",
    ProfileRole.RESULT_DELIVERY: "docspec.adapters.sinks:DurableDatasetSink",
}


def _put_blob_profile_state(
    controls: LocalJsonControlRepository, plan: ProcessingPlan, blobs: LocalContentAddressedBlobStore,
) -> ArtifactRef:
    profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
    state = {"profileId": profile.profile_id, "profileVersion": profile.version, "storageRoot": blobs.root.as_posix()}
    return controls.put(kind="profile-state", artifact_id=stable_urn("profile-state", state), value=state)


def _profile_limit(profile: RegisteredProfile, name: str) -> int:
    value = profile.description.limits.get(name)
    if type(value) is not int or value <= 0:
        raise ProfileError(f"profile {profile.description.profile_id} requires a positive integer {name} limit")
    return value


def _local_profiles(plan: ProcessingPlan, workspace: LocalWorkspace) -> dict[ProfileRole, RegisteredProfile]:
    registry = ProfileRegistry.from_directory(workspace.profile_directory)
    selected_ids = tuple(pin.profile_id for pin in plan.profiles.pins)
    if registry.select(selected_ids) != plan.profiles:
        raise ProfileError("processing plan profile pins differ from their machine descriptions")
    registered = {item.description.profile_id: item for item in registry.list()}
    selected_profiles: dict[ProfileRole, RegisteredProfile] = {}
    for pin in plan.profiles.pins:
        item = registered[pin.profile_id]
        if item.profile_set_id != _LOCAL_PROFILE_SET_ID or item.implementation_module != _LOCAL_PROFILE_MODULES[pin.role]:
            raise ProfileError(f"processing plan {pin.role.value} is not supported by the local composition")
        selected_profiles[pin.role] = item

    return selected_profiles


def _local_storage(
    roots: dict[str, Path],
    profiles: dict[ProfileRole, RegisteredProfile],
    producer: Producer,
    *,
    create: bool = True,
) -> tuple[
    LocalJsonControlRepository,
    LocalDocumentStoreRepository,
    LocalParquetRecordStorage,
    LocalContentAddressedBlobStore,
    LocalManifestDocumentCatalog,
]:
    controls = LocalJsonControlRepository(roots["controlRepository"], create=create)
    stores = _local_stores(roots["documentStores"], profiles[ProfileRole.DOCUMENT_STORE], create=create)
    record_profile = profiles[ProfileRole.RECORD_STORAGE]
    records = LocalParquetRecordStorage(
        roots["recordStorage"],
        create=create,
        max_member_bytes=_profile_limit(record_profile, "maxMemberBytes"),
        max_record_bytes=_profile_limit(record_profile, "maxRecordBytes"),
        max_root_bytes=_profile_limit(record_profile, "maxRootBytes"),
        max_merge_scratch_bytes=_profile_limit(record_profile, "maxMergeScratchBytes"),
    )
    blobs = LocalContentAddressedBlobStore(
        roots["blobStorage"],
        create=create,
        max_blob_bytes=_profile_limit(profiles[ProfileRole.BLOB_STORAGE], "maxObjectBytes"),
        stream_chunk_bytes=_profile_limit(profiles[ProfileRole.BLOB_STORAGE], "streamChunkBytes"),
    )
    release_limit = _profile_limit(profiles[ProfileRole.RELEASE_MANIFEST], "maxRootBytes")
    catalog_limit = _profile_limit(profiles[ProfileRole.DOCUMENT_CATALOG], "maxManifestBytes")
    catalog = LocalManifestDocumentCatalog(
        roots["documentCatalog"],
        create=create,
        records=records,
        stores=stores,
        controls=controls,
        producer=producer,
        blobs=blobs,
        max_release_bytes=min(release_limit, catalog_limit),
    )
    return controls, stores, records, blobs, catalog


def _local_stores(root: Path, profile: RegisteredProfile, *, create: bool = True) -> LocalDocumentStoreRepository:
    """Open just saved jobs when observation does not need other output stores."""
    return LocalDocumentStoreRepository(
        root, create=create,
        max_revision_bytes=_profile_limit(profile, "maxLedgerMemberBytes"),
        max_inline_bytes=_profile_limit(profile, "maxInlineBytes"),
        max_plan_ledger_bytes=_profile_limit(profile, "maxPlannedStoreLedgerBytes"),
        max_plan_record_bytes=_profile_limit(profile, "maxPlannedStoreRecordBytes"),
        max_plan_store_count=_profile_limit(profile, "maxPlannedStoreCount"),
    )
