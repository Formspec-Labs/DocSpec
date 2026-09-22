"""Public source-catalog reader contract: mapping access admits once, never re-derives or reconstructs items,
and matches the located-object view; streaming payload reads are bounded and close on partial consumption.

Covers refusing members or payloads changed after admission (even as valid canonical JSON), pin mismatches,
the shared domain refusals for malformed rows through both views, and the row-size limit on the mapping reader.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from rulespec_artifacts import ArtifactPin, admit_artifact, canonical_json_bytes

from docspec.adapters.catalog_artifact import derivation, payloads, reader, rows
from docspec.adapters.catalog_artifact.rules import _partition_id
from docspec.errors import IntegrityError, LimitExceededError
from docspec.source_catalog import (
    LocatedSourceCatalogMapping,
    SourceCatalogArtifactReader,
    SourceCatalogItem,
    open_admitted_source_catalog,
)
from tests.support.source_catalog import FakeSource, description, producer, record, renditions
from tests.support.source_catalog_builds import build


@pytest.fixture
def catalog(tmp_path: Path):
    ids = ("2026-00001", "2026-00002", "2026-00003")
    source = FakeSource(
        description(), tuple(record(value) for value in ids),
        tuple(row for value in ids for row in renditions(value)),
    )
    return build(tmp_path, source)


def _prohibited(*args: Any, **kwargs: Any) -> Any:
    """Fail if public mapping access repeats admission, derivation or SourceCatalogItem construction."""
    raise AssertionError("public mapping access must not repeat admission, derive, or construct SourceCatalogItem")


def test_public_mapping_access_admits_once_and_matches_objects(catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    store, result = catalog
    expected = list(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).located_items)
    expected_rows = [value.item.to_dict() for value in expected]
    actual_admit = reader.admit_artifact
    admissions = 0

    def admit(*args: Any, **kwargs: Any):
        nonlocal admissions
        admissions += 1
        return actual_admit(*args, **kwargs)

    monkeypatch.setattr(reader, "admit_artifact", admit)
    monkeypatch.setattr(derivation, "_derive_catalog", _prohibited)
    admitted = SourceCatalogArtifactReader(store, producer=producer()).admit_snapshot(result.reference)
    assert admitted.summary == result.summary
    assert list(admitted.open_snapshot().located_items) == expected

    monkeypatch.setattr(SourceCatalogItem, "from_dict", _prohibited)
    located = list(admitted.iter_located_mappings())
    assert all(isinstance(value, LocatedSourceCatalogMapping) for value in located)
    assert [value.item for value in located] == expected_rows
    assert [value.blob_ref for value in located] == [value.blob_ref for value in expected]
    located[0].item["normalizedMetadata"]["title"] = "caller-owned change"
    assert list(admitted.iter_mappings()) == expected_rows
    assert admissions == 1


def test_public_factory_uses_an_existing_admission(catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    store, result = catalog
    source = store.source_for(result.reference)
    blobs = store.blob_source()
    pin = ArtifactPin(result.reference.catalog_id, result.reference.digest)
    artifact = admit_artifact(source, blob_source=blobs, expected_pin=pin)
    monkeypatch.setattr(reader, "admit_artifact", _prohibited)
    monkeypatch.setattr(derivation, "_derive_catalog", _prohibited)
    monkeypatch.setattr(SourceCatalogItem, "from_dict", _prohibited)
    admitted = open_admitted_source_catalog(
        artifact, source, blob_source=blobs, producer=producer(), expected_pin=pin,
    )
    assert admitted.summary == result.summary
    assert [row["sourceItemId"] for row in admitted.iter_mappings()] == [
        "2026-00001", "2026-00002", "2026-00003",
    ]
    with pytest.raises(IntegrityError, match="expected pin"):
        open_admitted_source_catalog(
            artifact, source, blob_source=blobs, producer=producer(),
            expected_pin=ArtifactPin(pin.logical_id, "sha256:" + "a" * 64),
        )
    with pytest.raises(IntegrityError, match="expected pin"):
        open_admitted_source_catalog(
            replace(artifact, inputs=()), source, blob_source=blobs,
            producer=producer(), expected_pin=pin,
        )


@pytest.mark.parametrize("changed_member", ["artifact.json", "catalog-policy.json", "catalog-build-receipt.json", "manifest"])
def test_public_factory_refuses_members_changed_after_admission(
    catalog, tmp_path: Path, changed_member: str,
) -> None:
    store, result = catalog
    source = store.source_for(result.reference)
    blobs = store.blob_source()
    artifact = admit_artifact(source, blob_source=blobs)
    member = artifact.manifests[0].object_key if changed_member == "manifest" else changed_member
    path = tmp_path / result.reference.digest.removeprefix("sha256:") / member
    value = json.loads(path.read_bytes())
    # Valid canonical JSON, so refusal must bind to identity rather than rely
    # on the changed file merely becoming unparsable.
    value["tampered"] = True
    path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(IntegrityError):
        open_admitted_source_catalog(
            artifact, source, blob_source=blobs, producer=producer(), expected_pin=artifact.pin,
        )


class _StreamingBlobs:
    """Non-seekable public blob source double that records open/close counts and read sizes."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.open_count = 0
        self.closed_count = 0
        self.read_sizes: list[int] = []

    @contextmanager
    def open(self, blob_ref: str) -> Iterator[Any]:
        self.open_count += 1
        owner = self

        class ReadOnlyStream:
            def read(self, size: int) -> bytes:
                assert 0 < size <= 256 * 1024
                owner.read_sizes.append(size)
                return content.read(size)

        with BytesIO(self.payloads[blob_ref]) as content:
            try:
                yield ReadOnlyStream()
            finally:
                self.closed_count += 1


def _streaming_blobs(root: Path) -> _StreamingBlobs:
    """Index the local content-addressed blobs as a non-seekable streaming source."""
    return _StreamingBlobs({"sha256:" + path.name: path.read_bytes() for path in (root / ".blobs" / "sha256").iterdir()})


@pytest.mark.parametrize("objects", [False, True])
def test_nonseekable_provider_reads_are_bounded_and_close_on_partial_consumption(
    catalog, tmp_path: Path, objects: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, result = catalog
    source = store.source_for(result.reference)
    artifact = admit_artifact(source, blob_source=store.blob_source())
    blobs = _streaming_blobs(tmp_path)
    retained = []
    actual_temporary_file = payloads.TemporaryFile

    def temporary_file(*args, **kwargs):
        stream = actual_temporary_file(*args, **kwargs)
        retained.append(stream)
        return stream

    monkeypatch.setattr(payloads, "TemporaryFile", temporary_file)
    admitted = open_admitted_source_catalog(
        artifact, source, blob_source=blobs, producer=producer(), expected_pin=artifact.pin,
    )
    assert blobs.open_count == 0
    # Keep the snapshot alive: closing its items iterator must close the
    # located stream rather than depend on garbage collection to release it.
    snapshot = admitted.open_snapshot()
    stream = snapshot.items if objects else admitted.iter_mappings()
    first = next(stream)
    assert (first.source_item_id if objects else first["sourceItemId"]) == "2026-00001"
    assert blobs.open_count == blobs.closed_count > 0
    assert any(not file.closed for file in retained)
    stream.close()
    assert all(file.closed for file in retained)
    assert blobs.read_sizes
    assert len(list(admitted.iter_mappings())) == result.summary.item_count
    assert blobs.open_count == blobs.closed_count


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("objects", [True, False])
def test_changed_payload_is_refused_before_any_row(catalog, tmp_path: Path, local: bool, objects: bool) -> None:
    store, result = catalog
    source = store.source_for(result.reference)
    artifact = admit_artifact(source, blob_source=store.blob_source())
    blobs = store.blob_source() if local else _streaming_blobs(tmp_path)
    admitted = open_admitted_source_catalog(
        artifact, source, blob_source=blobs, producer=producer(), expected_pin=artifact.pin,
    )
    path = next((tmp_path / ".blobs" / "sha256").iterdir())
    blob_ref = "sha256:" + path.name
    original = path.read_bytes()
    changed = original.replace(b'"title":', b'"other":', 1)
    assert len(changed) == len(original) and changed != original
    if local:
        path.write_bytes(changed)
    else:
        blobs.payloads[blob_ref] = changed
    stream = admitted.open_snapshot().located_items if objects else admitted.iter_located_mappings()
    with pytest.raises(IntegrityError, match="payload"):
        next(stream)


@pytest.mark.parametrize("mutation", ["duplicate-rendition", "no-candidate", "blank-id"])
def test_mapping_and_object_rows_share_domain_refusals(catalog, mutation: str) -> None:
    store, result = catalog
    row = next(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items).to_dict()
    if mutation == "duplicate-rendition":
        row["candidateRenditions"] *= 2
    elif mutation == "no-candidate":
        row["candidateRenditions"] = []
    else:
        row["documentId"] = " "
    for as_dict in (False, True):
        with pytest.raises(IntegrityError, match="source-catalog row 0 is invalid"):
            list(rows._iter_partition_stream(
                BytesIO(canonical_json_bytes(row) + b"\n"),
                partition_id=_partition_id(row["sourceItemId"]), record_count=1,
                validate=True, with_raw=False, as_dict=as_dict,
            ))


def test_public_mapping_reader_keeps_row_size_limit(catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    store, result = catalog
    admitted = SourceCatalogArtifactReader(store, producer=producer()).admit_snapshot(result.reference)
    monkeypatch.setattr(rows, "MAX_CATALOG_ROW_BYTES", 128)
    with pytest.raises(LimitExceededError, match="row exceeds"):
        next(admitted.iter_mappings())
