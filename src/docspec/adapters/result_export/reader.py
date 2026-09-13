"""Admit a self-contained active result without opening its former workspace."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO, Self

from rulespec_artifacts import (
    ArtifactPin, ArtifactVerificationError, LocalMemberSource, MemberDescriptor, MemberSourceError, Producer,
    VerifiedArtifact, admit_artifact, iter_member_descriptors,
)

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.controls import parse_control_artifact
from docspec.domain.delivery import core_delivery_schemas, verify_logical_release_layers
from docspec.domain.identity import identity_digest, parse_canonical_json, thaw_json
from docspec.domain.references import ArtifactRef, BlobRef
from docspec.errors import IntegrityError, LimitExceededError

from .admission import collection, observe_active_items
from .io import (
    ADMISSIONS, CONTROL_SCHEMA, INDEX_KEY, INDEX_SCHEMA, MANIFEST_BYTES, MANIFEST_KEY,
    RECORD_BYTES, ROOT_BYTES, ROW_SCHEMA, read_mapping, require_limit, rows, scratch, verified_open,
)
from .references import evidence_references


class AdmittedResultExport:
    """A closeable read-only view whose scratch index contains no dataset state.

    Use a context manager, or call ``close``. Byte access is restricted to exact
    references found in this artifact. Reads recheck consumed members against
    their admitted descriptors; original source/plan provenance is not fetched.
    """

    def __init__(self, source, workspace) -> None:
        self._source, self._workspace = source, workspace
        self._closed = False
        self._layers: dict[str, dict[str, Any]] = {}
        self._summary: dict[str, Any] = {}
        self._pin: ArtifactPin | None = None

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._workspace.__exit__(None, None, None)
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("result export view is closed")

    @property
    def pin(self) -> ArtifactPin:
        self._require_open()
        if self._pin is None:
            raise RuntimeError("result export admission is not complete")
        return self._pin

    @property
    def summary(self) -> dict[str, Any]:
        self._require_open()
        return deepcopy(self._summary)

    @property
    def layer_kinds(self) -> tuple[str, ...]:
        self._require_open()
        return tuple(self._layers)

    def _member(self, key: str) -> MemberDescriptor:
        self._require_open()
        raw = self._workspace.lookup_record("members", key)
        if raw is None:
            raise IntegrityError(f"export is missing a referenced member: {key}")
        return MemberDescriptor.from_dict(raw, path="export/member")

    def _mark_used(self, key: str) -> None:
        if self._workspace.lookup_record("used", key) is None:
            self._workspace.add_record("used", identity=key, source_item_id=key, record={"key": key})

    def _reference(self, reference: ArtifactRef | BlobRef, *, admit: bool = False) -> MemberDescriptor:
        member = self._member(reference.locator)
        role = "evidence" if isinstance(reference, ArtifactRef) else "blob"
        if (member.sha256, member.byte_size, member.role) != (reference.digest, reference.byte_size, role):
            raise IntegrityError("export reference differs from its member descriptor")
        if isinstance(reference, BlobRef) and reference.locator != LocalContentAddressedBlobStore._locator(reference.digest):
            raise IntegrityError("export blob locator differs from its digest")
        identity = identity_digest(reference.to_dict())
        if self._workspace.lookup_record("references", identity) is None:
            if not admit:
                raise IntegrityError("reference does not belong to this admitted export")
            self._workspace.add_record("references", identity=identity,
                source_item_id=reference.locator, record=reference.to_dict())
        if admit:
            self._mark_used(reference.locator)
        return member

    @contextmanager
    def open_blob(self, reference: BlobRef) -> Iterator[BinaryIO]:
        """Open a verified, seekable blob; the member and total artifact bound apply."""
        with verified_open(self._source, self._reference(reference)) as stream:
            yield stream

    def read_blob(self, reference: BlobRef, *, max_bytes: int) -> bytes:
        """Read one blob only when its full size fits the caller's memory bound."""
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if reference.byte_size > max_bytes:
            raise LimitExceededError("export blob exceeds max_bytes")
        with self.open_blob(reference) as stream:
            return stream.read(max_bytes + 1)

    def read_evidence(self, reference: ArtifactRef) -> dict[str, Any]:
        """Load one exact typed control artifact, bounded at 8 MiB."""
        member = self._reference(reference)
        if member.byte_size > RECORD_BYTES:
            raise LimitExceededError("export control artifact exceeds its byte limit")
        with verified_open(self._source, member) as stream:
            return parse_control_artifact(reference, stream.read(RECORD_BYTES + 1))

    def load(self, reference: ArtifactRef) -> dict[str, Any]:
        """Supply the existing control-reader port to shared receipt verification."""
        return self.read_evidence(reference)

    def records(self, layer_kind: str) -> Iterator[dict[str, Any]]:
        """Stream original delivery rows; closing the iterator releases its file."""
        self._require_open()
        if layer_kind not in self._layers:
            raise ValueError(f"result export has no layer {layer_kind!r}")
        yield from rows(self._source, self._member(self._layers[layer_kind]["objectKey"]))

    def _admit_evidence(self, reference: ArtifactRef) -> None:
        already = self._workspace.lookup_record("references", identity_digest(reference.to_dict())) is not None
        self._reference(reference, admit=True)
        if not already:
            for dependency in evidence_references(self.read_evidence(reference)):
                self._admit_evidence(dependency)

    def _index_rows(self, kind):
        with closing(self.records(kind)) as values:
            for row in values:
                if kind.startswith("derived:") and row["payload"].get("schemaId") != self._layers[kind]["schemaId"]:
                    raise IntegrityError("export derived record differs from its layer schema")
                target = "sources" if kind == "source-items" else collection(row["sourceItemId"], kind)
                self._workspace.add_record(target, identity=row["recordId"], source_item_id=row["sourceItemId"], record=row)
                yield row

    def _admit(self, artifact: VerifiedArtifact, _source, *, producer: Producer) -> None:
        root = artifact.root
        expected_spec = {
            "schemaId", "retainedArtifactDigest", "admissionId", "populationScope", "evidenceScope",
        }
        spec = root["spec"]
        admission = next((value for value in ADMISSIONS
            if spec.get("admissionId") == f"urn:docspec:export-admission:{value}:1"), None)
        if (root["kind"] != "docspec-result-export" or root["producer"] != producer.as_dict()
            or set(spec) != expected_spec or spec["schemaId"] != "urn:docspec:result-export:1.0"
            or spec["populationScope"] != "retained-active-result"
            or spec["evidenceScope"] != "active-output-and-stage-receipts" or admission is None
            or len(artifact.inputs) != 1 or artifact.inputs[0].role != "retained-result"
            or artifact.inputs[0].artifact_digest != spec["retainedArtifactDigest"]):
            raise IntegrityError("result export kind, producer, source identity or admission is invalid")
        if len(artifact.manifests) != 1 or (
            artifact.manifests[0].scope_kind, artifact.manifests[0].scope_id, artifact.manifests[0].object_key
        ) != ("global", "active-result", MANIFEST_KEY):
            raise IntegrityError("result export has an invalid member manifest")
        with closing(iter_member_descriptors(artifact, self._source)) as descriptors:
            for member in descriptors:
                expected = {"index": ("application/json", INDEX_SCHEMA),
                    "records": ("application/x-ndjson", ROW_SCHEMA),
                    "evidence": ("application/json", CONTROL_SCHEMA), "blob": ("application/octet-stream", None)}
                if (member.object_key is None or member.role not in expected
                    or (member.media_type, member.schema_id) != expected[member.role]
                    or (member.record_count is not None) != (member.role == "records")
                    or (member.role == "index" and member.object_key != INDEX_KEY)):
                    raise IntegrityError("result export member has an invalid role or schema")
                self._workspace.add_record("members", identity=member.object_key,
                    source_item_id=member.object_key, record=member.as_dict())
        index = read_mapping(self._source, self._member(INDEX_KEY), max_bytes=ROOT_BYTES)
        if (set(index) != {"format", "formatVersion", "layers"}
            or index["format"] != "docspec-result-export-index" or index["formatVersion"] != "1.0"
            or not isinstance(index["layers"], list)):
            raise IntegrityError("result export index has an invalid closed shape")
        self._mark_used(INDEX_KEY)
        core = core_delivery_schemas()
        for layer in index["layers"]:
            if not isinstance(layer, dict) or set(layer) != {"kind", "schemaId", "objectKey", "recordCount"}:
                raise IntegrityError("result export layer has an invalid closed shape")
            kind = layer["kind"]
            if (not isinstance(kind, str) or kind in self._layers
                or (kind not in core and not kind.startswith("derived:"))
                or not isinstance(layer["schemaId"], str)
                or (kind in core and layer["schemaId"] != core[kind].schema_id)
                or type(layer["recordCount"]) is not int or layer["recordCount"] < 0
                or layer["objectKey"] != f"records/{identity_digest(kind).removeprefix('sha256:')}.jsonl"):
                raise IntegrityError("result export layer identity or schema is invalid")
            member = self._member(layer["objectKey"])
            if member.role != "records" or member.record_count != layer["recordCount"]:
                raise IntegrityError("result export layer differs from its member")
            self._layers[kind] = layer
            self._mark_used(member.object_key)
        if not set(core) <= set(self._layers) or list(self._layers) != sorted(self._layers):
            raise IntegrityError("result export requires all core layers in sorted order")
        with ExitStack() as stack:
            layers = {kind: stack.enter_context(closing(self._index_rows(kind))) for kind in self._layers}
            verify_logical_release_layers(layers, verify_artifact=self._admit_evidence,
                verify_blob=lambda reference: self._reference(reference, admit=True))
        report = observe_active_items(self, self._workspace, admission)
        with closing(self._workspace.stream_records("members")) as descriptors:
            for raw in descriptors:
                if self._workspace.lookup_record("used", raw["objectKey"]) is None:
                    raise IntegrityError("result export contains an unreferenced payload member")
        self._pin = artifact.pin
        self._summary = {**report, "retainedResult": artifact.inputs[0].as_dict(),
            "layers": deepcopy(index["layers"]), "memberCount": artifact.member_count,
            "embeddedPayloadBytes": artifact.total_member_byte_size,
            "provenanceScope": "Source catalogs, prior results and provider resources remain external identity pins."}


def open_result_export(
    path: Path, *, expected_pin: ArtifactPin, producer: Producer, max_output_bytes: int,
) -> AdmittedResultExport:
    """Open an exact exported result using only its directory and accepted pins.

    ``max_output_bytes`` includes root, manifest and payload bytes. JSON rows
    and controls are limited to 8 MiB; per-item metadata is limited to 64 MiB.
    Disposable index input is bounded at four times the admitted artifact bound.
    Use the returned view as a context manager to release its scratch index.
    """
    require_limit(max_output_bytes)
    workspace = scratch(4 * max_output_bytes)
    workspace.__enter__()
    view = None
    try:
        source = LocalMemberSource(Path(path))
        with source.open("artifact.json") as stream:
            payload = stream.read(ROOT_BYTES + 1)
        if len(payload) > ROOT_BYTES:
            raise LimitExceededError("export root exceeds its byte limit")
        root = thaw_json(parse_canonical_json(payload, label="result export root", file_form=False))
        sizes = [root["counts"]["totalMemberByteSize"],
            *(value["byteSize"] for value in root["memberManifests"]),
            *(value["totalMemberByteSize"] for value in root["memberManifests"])]
        if any(type(value) is not int or value < 0 for value in sizes):
            raise IntegrityError("export root declares invalid byte counts")
        manifest_bytes = sum(value["byteSize"] for value in root["memberManifests"])
        declared_payload_bytes = max(root["counts"]["totalMemberByteSize"],
            sum(value["totalMemberByteSize"] for value in root["memberManifests"]))
        if len(payload) + manifest_bytes + declared_payload_bytes > max_output_bytes:
            raise LimitExceededError("result export exceeds max_output_bytes")
        view = AdmittedResultExport(source, workspace)
        admit_artifact(source, expected_pin=expected_pin, root_byte_limit=ROOT_BYTES,
            manifest_byte_limit=min(MANIFEST_BYTES, max_output_bytes),
            semantic_verifier=lambda artifact, member_source: view._admit(artifact, member_source, producer=producer))
        return view
    except BaseException as error:
        if view is None:
            workspace.__exit__(None, None, None)
        else:
            view.close()
        if isinstance(error, (ArtifactVerificationError, MemberSourceError, KeyError, TypeError, ValueError)):
            raise IntegrityError(f"result export is invalid: {error}") from error
        raise
