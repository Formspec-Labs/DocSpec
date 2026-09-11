from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from rulespec_artifacts import canonical_json_bytes, stamp_root

from docspec.domain.identity import (
    canonical_json_file_bytes,
)
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, StoreRef
from docspec.errors import IntegrityError, StaleBaseError
from tests.support.document_catalog import (
    BASE_ROWS,
    LAYER_KIND,
    _commit_run,
    _platform,
    _registered_catalog_profiles,
)

SUCCESSOR_ROWS = (
    {"recordId": "alpha", "sourceItemId": "source-alpha", "value": 10},
    {"recordId": "charlie", "sourceItemId": "source-charlie", "value": 3},
)


def test_every_registered_catalog_profile_opens_compares_stages_and_commits_the_shared_release(
    tmp_path: Path,
) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        assert catalog.current() is None

        base = _commit_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        assert catalog.current() == base.reference
        base_root = json.loads((catalog.root / base.reference.locator).read_text(encoding="utf-8"))
        assert "supersedes" not in base_root
        opened = catalog.open(base.reference)
        assert opened.previous_release is None
        assert opened.reference(base.reference.locator, base.reference.digest) == base.reference

        reader = catalog.open_reader(base.reference)
        assert reader.lookup(layer_kind=LAYER_KIND, record_id="alpha") == BASE_ROWS[0]
        assert list(reader.scan(layer_kind=LAYER_KIND)) == list(BASE_ROWS)
        assert list(reader.scan_source(layer_kind=LAYER_KIND, source_item_id="source-bravo")) == [BASE_ROWS[1]]
        assert catalog.lookup(base.reference, layer_kind=LAYER_KIND, record_id="charlie") is None

        successor = _commit_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)
        assert catalog.current() == successor.reference
        successor_root = json.loads(
            (catalog.root / successor.reference.locator).read_text(encoding="utf-8")
        )
        assert successor_root["supersedes"] == {
            "artifactDigest": base.reference.digest,
            "logicalId": base.reference.release_id,
            "reason": "advance document catalog from previousRelease",
        }
        assert catalog.open(successor.reference).previous_release == base.reference
        assert sorted(catalog.compare(base.reference, successor.reference, layer_kind=LAYER_KIND)) == [
            ("alpha", "changed"),
            ("bravo", "deleted"),
            ("charlie", "added"),
        ]
        assert list(catalog.compare(base.reference, base.reference, layer_kind=LAYER_KIND)) == []


def test_every_registered_catalog_profile_replays_conflicts_and_refuses_stale_or_unsealed_commits(
    tmp_path: Path,
) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _commit_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        successor = _commit_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)

        replayed = catalog.commit(
            successor.artifact,
            expected_base=base.reference,
            stores=(successor.sealed_store,),
        )
        assert replayed == successor.reference, "an identical replay must return the committed head unchanged"

        with pytest.raises(IntegrityError, match="unsealed document store"):
            catalog.commit(successor.artifact, expected_base=base.reference, stores=(successor.planned_store,))

        with pytest.raises(StaleBaseError, match="differs from the expected base"):
            _commit_run(platform, run_tag="stale", rows=BASE_ROWS, base=base.reference)


def test_every_registered_catalog_profile_replays_the_exact_stage_after_publish_before_head_update(
    tmp_path: Path,
) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _commit_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        captured: list[tuple[ArtifactRef, DocumentReleaseRef | None, tuple[StoreRef, ...]]] = []
        original_commit = catalog.commit

        def fail_after_publish(
            staged: ArtifactRef,
            *,
            expected_base: DocumentReleaseRef | None,
            stores,
        ) -> DocumentReleaseRef:
            store_tuple = tuple(stores)
            captured.append((staged, expected_base, store_tuple))
            original_write_current = catalog._write_current

            def fail_current(_reference: DocumentReleaseRef) -> None:
                raise OSError("simulated failure after immutable publication")

            catalog._write_current = fail_current
            try:
                return original_commit(staged, expected_base=expected_base, stores=store_tuple)
            finally:
                catalog._write_current = original_write_current

        catalog.commit = fail_after_publish  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="after immutable publication"):
                _commit_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)
        finally:
            catalog.commit = original_commit  # type: ignore[method-assign]

        assert catalog.current() == base.reference
        assert len(captured) == 1
        staged, expected_base, stores = captured[0]
        assert not (catalog.root / staged.locator).exists()
        replayed = catalog.commit(staged, expected_base=expected_base, stores=stores)
        assert catalog.current() == replayed
        assert catalog.open(replayed).previous_release == base.reference


def test_document_catalog_independent_admission_rejects_cross_series_and_pointer_tamper(
    tmp_path: Path,
) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _commit_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        successor = _commit_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)

        def copy_with_supersedes(reference: DocumentReleaseRef, supersedes) -> DocumentReleaseRef:
            source_directory = (catalog.root / reference.locator).parent
            root = json.loads((source_directory / "artifact.json").read_text(encoding="utf-8"))
            root["supersedes"] = supersedes
            stamped = stamp_root(root)
            digest = stamped["artifactDigest"]
            destination = catalog.root / catalog._artifact_directory("releases", digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_directory, destination)
            (destination / "artifact.json").write_bytes(canonical_json_bytes(stamped))
            return DocumentReleaseRef(
                stamped["logicalId"],
                catalog._release_locator(digest),
                digest,
            )

        initial_with_predecessor = copy_with_supersedes(
            base.reference,
            {
                "artifactDigest": "sha256:" + "8" * 64,
                "logicalId": "urn:spicy:artifact:derivation:" + "9" * 64,
                "reason": "invalid predecessor for an initial release",
            },
        )
        with pytest.raises(IntegrityError, match="initial document release"):
            catalog.open(initial_with_predecessor)

        cross_series = copy_with_supersedes(
            successor.reference,
            {
                "artifactDigest": "sha256:" + "6" * 64,
                "logicalId": "urn:spicy:artifact:derivation:" + "7" * 64,
                "reason": "unrelated document series",
            },
        )
        with pytest.raises(IntegrityError, match="differs from previousRelease"):
            catalog.open(cross_series)

        pointer_path = catalog.root / "document-catalog/current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["release"]["digest"] = "sha256:" + "0" * 64
        pointer_path.write_bytes(canonical_json_file_bytes(pointer))
        with pytest.raises(IntegrityError):
            catalog.current()
