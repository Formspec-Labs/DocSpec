"""Publish an existing retained active result without executing any work."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import closing
from pathlib import Path

from rulespec_artifacts import (
    ArtifactInput, ArtifactPin, LocalMemberSource, MemberDescriptor, Producer,
    build_artifact_root, describe_member, publish_directory_no_replace, write_member_manifest,
)

from docspec.adapters.result_export.io import (
    ADMISSIONS, CONTROL_SCHEMA, INDEX_KEY, INDEX_SCHEMA, MANIFEST_BYTES, MANIFEST_KEY,
    RECORD_BYTES, ROOT_BYTES, ROW_SCHEMA, require_limit, scratch,
)
from docspec.adapters.result_export.references import evidence_references, row_references
from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.controls import LocalJsonControlRepository
from docspec.adapters.storage.files import _contained, _read_exact, _verify_artifact_bytes
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, identity_digest
from docspec.domain.references import ArtifactRef, DocumentReleaseRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.document_catalog import DocumentCatalogReader


def export_result(
    reader: DocumentCatalogReader, controls: LocalJsonControlRepository,
    blobs: LocalContentAddressedBlobStore, release_ref: DocumentReleaseRef,
    destination: Path, *, admission: str, producer: Producer, max_output_bytes: int,
) -> ArtifactPin:
    """Copy only active output dependencies, then admit and publish all-or-nothing."""
    from .reader import open_result_export

    require_limit(max_output_bytes)
    if admission not in ADMISSIONS:
        raise ValueError(f"admission must be one of {ADMISSIONS}")
    # Validate producer spelling before opening an output directory.
    Producer.from_dict(producer.as_dict(), path="export/producer")
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise IntegrityError("export destination must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    working = Path(tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent))
    try:
        with scratch(max_output_bytes) as members:
            total = 0

            def charge(size):
                nonlocal total
                total += size
                if total > max_output_bytes:
                    raise LimitExceededError("result export exceeds max_output_bytes")

            def add_member(descriptor):
                members.add_record("members", identity=descriptor.object_key,
                    source_item_id=descriptor.object_key, record=descriptor.as_dict())

            def embed(reference):
                role = "evidence" if isinstance(reference, ArtifactRef) else "blob"
                previous = members.lookup_record("members", reference.locator)
                if previous is not None:
                    if (previous["sha256"], previous["byteSize"], previous["role"]) != (
                        reference.digest, reference.byte_size, role,
                    ):
                        raise IntegrityError("export references conflict for one member")
                    return
                charge(reference.byte_size)
                path = _contained(working, reference.locator, create_parents=True)
                if isinstance(reference, ArtifactRef):
                    value = controls.load(reference)
                    content = _read_exact(controls.root, reference.locator, max_bytes=RECORD_BYTES)
                    _verify_artifact_bytes(reference, content)
                    with path.open("xb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                else:
                    if reference.locator != blobs._locator(reference.digest):
                        raise IntegrityError("export blob locator differs from its digest")
                    with path.open("xb") as stream, closing(blobs.read(reference)) as chunks:
                        for chunk in chunks:
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                add_member(MemberDescriptor(
                    object_key=reference.locator, role=role,
                    media_type="application/json" if role == "evidence" else "application/octet-stream",
                    byte_size=reference.byte_size, sha256=reference.digest,
                    schema_id=CONTROL_SCHEMA if role == "evidence" else None,
                ))
                if isinstance(reference, ArtifactRef):
                    for dependency in evidence_references(value):
                        embed(dependency)

            layers = []
            for layer in sorted(reader.release.active_layers, key=lambda value: value.layer_kind):
                key = f"records/{identity_digest(layer.layer_kind).removeprefix('sha256:')}.jsonl"
                path = _contained(working, key, create_parents=True)
                count = 0
                with path.open("xb") as stream, closing(reader.scan(layer_kind=layer.layer_kind)) as values:
                    for row in values:
                        content = canonical_json_file_bytes(row)
                        if len(content) > RECORD_BYTES:
                            raise LimitExceededError("export row exceeds its byte limit")
                        charge(len(content))
                        stream.write(content)
                        count += 1
                        for reference in row_references(layer.layer_kind, row):
                            embed(reference)
                    stream.flush()
                    os.fsync(stream.fileno())
                if count != layer.record_count:
                    raise IntegrityError("retained layer count changed during export")
                add_member(describe_member(LocalMemberSource(working), object_key=key,
                    role="records", media_type="application/x-ndjson", record_count=count, schema_id=ROW_SCHEMA))
                layers.append({"kind": layer.layer_kind, "schemaId": layer.schema_id,
                    "objectKey": key, "recordCount": count})

            index = {"format": "docspec-result-export-index", "formatVersion": "1.0", "layers": layers}
            payload = canonical_json_file_bytes(index)
            if len(payload) > ROOT_BYTES:
                raise LimitExceededError("result export index exceeds its byte limit")
            charge(len(payload))
            with (working / INDEX_KEY).open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            add_member(describe_member(LocalMemberSource(working), object_key=INDEX_KEY,
                role="index", media_type="application/json", schema_id=INDEX_SCHEMA))
            with (working / MANIFEST_KEY).open("xb") as stream, closing(members.stream_records("members")) as values:
                if total >= max_output_bytes:
                    raise LimitExceededError("result export leaves no room for its manifest")
                manifest = write_member_manifest(stream, scope_kind="global", scope_id="active-result",
                    object_key=MANIFEST_KEY,
                    members=(MemberDescriptor.from_dict(value, path="export/member") for value in values),
                    byte_limit=min(MANIFEST_BYTES, max_output_bytes - total))
                stream.flush()
                os.fsync(stream.fileno())
            charge(manifest.byte_size)
            root = build_artifact_root(kind="docspec-result-export", producer=producer,
                spec={"schemaId": "urn:docspec:result-export:1.0",
                    "retainedArtifactDigest": release_ref.digest,
                    "admissionId": f"urn:docspec:export-admission:{admission}:1",
                    "populationScope": "retained-active-result",
                    "evidenceScope": "active-output-and-stage-receipts"},
                inputs=(ArtifactInput("retained-result", release_ref.release_id, release_ref.digest),),
                manifests=(manifest,))
            payload = canonical_json_bytes(root)
            if len(payload) > ROOT_BYTES:
                raise LimitExceededError("result export root exceeds its byte limit")
            charge(len(payload))
            with (working / "artifact.json").open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            pin = ArtifactPin(root["logicalId"], root["artifactDigest"])
            with open_result_export(working, expected_pin=pin, producer=producer, max_output_bytes=max_output_bytes):
                pass
            try:
                publish_directory_no_replace(working, destination)
            except FileExistsError:
                with open_result_export(destination, expected_pin=pin, producer=producer, max_output_bytes=max_output_bytes):
                    pass
            return pin
    finally:
        if working.exists():
            shutil.rmtree(working)
