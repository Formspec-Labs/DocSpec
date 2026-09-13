from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
from rulespec_artifacts import canonical_json_bytes, stamp_root

from docspec.domain.identity import (
    canonical_json_file_bytes,
)
from docspec.application.commit import ReleaseCommitService
from docspec.domain.references import DocumentReleaseRef
from docspec.errors import IntegrityError, StaleBaseError
from tests.support.document_catalog import (
    BASE_ROWS,
    LAYER_KIND,
    _save_run,
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

        base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
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

        successor = _save_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)
        assert catalog.current() == successor.reference
        successor_root = json.loads(
            (catalog.root / successor.reference.locator).read_text(encoding="utf-8")
        )
        assert successor_root["supersedes"] == {
            "artifactDigest": base.reference.digest,
            "logicalId": base.reference.release_id,
            "reason": "derive document state from previousRelease",
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
        base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        successor = _save_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)

        replayed = catalog.commit(
            successor.artifact,
            expected_base=base.reference,
            stores=(successor.sealed_store,),
        )
        assert replayed == successor.reference, "an identical replay must return the committed head unchanged"

        with pytest.raises(IntegrityError, match="unsealed document store"):
            catalog.commit(successor.artifact, expected_base=base.reference, stores=(successor.planned_store,))

        with pytest.raises(StaleBaseError, match="differs from the expected current head"):
            _save_run(platform, run_tag="stale", rows=BASE_ROWS, base=base.reference)


def test_every_registered_catalog_profile_replays_the_exact_stage_after_publish_before_head_update(
    tmp_path: Path,
) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        captured: list[DocumentReleaseRef] = []
        original_write_current = catalog._write_current

        def fail_after_publish(reference: DocumentReleaseRef) -> None:
            captured.append(reference)
            raise OSError("simulated failure after immutable publication")

        catalog._write_current = fail_after_publish  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="after immutable publication"):
                _save_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)
        finally:
            catalog._write_current = original_write_current  # type: ignore[method-assign]

        assert catalog.current() == base.reference
        assert len(captured) == 1
        retained = captured[0]
        assert catalog.open(retained).previous_release == base.reference
        replayed = catalog.select(retained, expected_current=base.reference)
        assert catalog.current() == replayed
        assert catalog.open(replayed).previous_release == base.reference


def test_document_catalog_independent_admission_rejects_cross_series_and_pointer_tamper(
    tmp_path: Path,
) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        successor = _save_run(platform, run_tag="successor", rows=SUCCESSOR_ROWS, base=base.reference)

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


def test_every_catalog_profile_retains_alternatives_without_selecting_them(tmp_path: Path) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None, select_current=False)
        assert catalog.current() is None
        assert catalog.select(base.reference, expected_current=None) == base.reference

        first = _save_run(
            platform, run_tag="first", rows=SUCCESSOR_ROWS, base=base.reference, select_current=False,
        )
        assert catalog.current() == base.reference
        assert catalog.select(first.reference, expected_current=base.reference) == first.reference
        second = _save_run(
            platform, run_tag="second", rows=BASE_ROWS, base=base.reference, select_current=False,
        )
        assert first.reference != second.reference
        assert catalog.current() == first.reference
        assert catalog.open(first.reference).previous_release == base.reference
        assert catalog.open(second.reference).previous_release == base.reference
        assert list(catalog.scan(first.reference, layer_kind=LAYER_KIND)) == list(SUCCESSOR_ROWS)
        assert list(catalog.scan(second.reference, layer_kind=LAYER_KIND)) == list(BASE_ROWS)
        assert sorted(catalog.compare(first.reference, second.reference, layer_kind=LAYER_KIND)) == [
            ("alpha", "changed"), ("bravo", "added"), ("charlie", "deleted"),
        ]

        service = ReleaseCommitService(
            plan_ref=second.plan_ref, controls=platform.controls,
            records=platform.records, document_catalog=catalog,
        )
        assert service.retain_release(base.reference, second.run_ref) == second.reference
        assert catalog.retain(second.artifact, stores=iter((second.sealed_store,))) == second.reference
        assert catalog.current() == first.reference
        assert catalog.select(first.reference, expected_current=base.reference) == first.reference
        with pytest.raises(StaleBaseError, match="expected current head"):
            catalog.select(second.reference, expected_current=base.reference)
        assert catalog.current() == first.reference
        assert catalog.open(second.reference).previous_release == base.reference
        assert catalog.select(second.reference, expected_current=first.reference) == second.reference
        assert catalog.current() == second.reference
        assert catalog.open(first.reference).previous_release == base.reference
        assert catalog.open(second.reference).previous_release == base.reference


@pytest.mark.parametrize("damaged", ["root", "run receipt", "store"])
def test_selection_reverifies_retained_artifacts_and_dependencies(tmp_path: Path, damaged: str) -> None:
    for registered in _registered_catalog_profiles():
        platform = _platform(registered, tmp_path / registered.description.implementation_id)
        catalog = platform.catalog
        base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
        candidate = _save_run(
            platform, run_tag="candidate", rows=SUCCESSOR_ROWS, base=base.reference, select_current=False,
        )
        target = {
            "root": catalog.root / candidate.reference.locator,
            "run receipt": platform.controls.root / candidate.run_ref.locator,
            "store": platform.stores.root / candidate.sealed_store.locator,
        }[damaged]
        target.write_bytes(target.read_bytes() + b"tampered")
        with pytest.raises(IntegrityError):
            catalog.select(candidate.reference, expected_current=base.reference)
        assert catalog.current() == base.reference
        assert not (catalog.root / "document-catalog/.commit.lock").exists()


def test_retention_refuses_wrong_store_evidence_and_unrelated_requested_base(tmp_path: Path) -> None:
    registered = _registered_catalog_profiles()[0]
    platform = _platform(registered, tmp_path)
    catalog = platform.catalog
    base = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
    candidate = _save_run(
        platform, run_tag="candidate", rows=SUCCESSOR_ROWS, base=base.reference, select_current=False,
    )
    with pytest.raises(IntegrityError, match="unsealed"):
        catalog.retain(candidate.artifact, stores=iter((candidate.planned_store,)))
    with pytest.raises(IntegrityError, match="sorted and distinct"):
        catalog.retain(candidate.artifact, stores=iter((candidate.sealed_store, candidate.sealed_store)))
    with pytest.raises(IntegrityError, match="receipt set"):
        catalog.retain(candidate.artifact, stores=iter((base.sealed_store,)))
    service = ReleaseCommitService(
        plan_ref=candidate.plan_ref, controls=platform.controls,
        records=platform.records, document_catalog=catalog,
    )
    with pytest.raises(IntegrityError, match="plan or base"):
        service.retain_release(None, candidate.run_ref)
    assert catalog.current() == base.reference


def test_retention_resolves_an_already_moved_stage_after_acquiring_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _platform(_registered_catalog_profiles()[0], tmp_path)
    catalog = platform.catalog
    saved = _save_run(platform, run_tag="base", rows=BASE_ROWS, base=None, select_current=False)
    staged = catalog.stage(catalog.open(saved.reference))
    original_lock = catalog._write_lock

    @contextmanager
    def earlier_writer(release_id):
        with monkeypatch.context() as writer:
            writer.setattr(catalog, "_write_lock", original_lock)
            assert catalog.retain(staged, stores=(saved.sealed_store,)) == saved.reference
        assert not (catalog.root / staged.locator).exists()
        with original_lock(release_id):
            yield

    monkeypatch.setattr(catalog, "_write_lock", earlier_writer)
    assert catalog.retain(staged, stores=iter((saved.sealed_store,))) == saved.reference
    assert catalog.current() is None
    assert list(catalog.scan(saved.reference, layer_kind=LAYER_KIND)) == list(BASE_ROWS)
