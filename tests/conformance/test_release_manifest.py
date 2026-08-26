from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from rulespec_artifacts import (
    ROOT_OBJECT_KEY,
    ArtifactPin,
    LocalMemberSource,
    admit_artifact,
    canonical_json_bytes,
)

from docspec.adapters.platform_artifact import RELEASE_STATE_KEY
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import DocumentReleaseRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.profile_registry import ProfileRegistry

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_catalog_contract = importlib.import_module("tests.conformance.test_document_catalog_contract")

BASE_ROWS = _catalog_contract.BASE_ROWS


def _committed_platform(tmp_path: Path):
    registered = _catalog_contract._registered_catalog_profiles()[0]
    platform = _catalog_contract._platform(registered, tmp_path / "platform")
    committed = _catalog_contract._commit_run(platform, run_tag="base", rows=BASE_ROWS, base=None)
    return registered, platform, committed


def test_every_catalog_profile_publishes_the_canonical_release_root(tmp_path: Path) -> None:
    registered, platform, committed = _committed_platform(tmp_path)
    catalog = platform.catalog
    reference = committed.reference
    distribution = (catalog.root / reference.locator).parent

    artifact = admit_artifact(
        LocalMemberSource(distribution),
        expected_pin=ArtifactPin(reference.release_id, reference.digest),
    )
    assert artifact.root["kind"] == "derivation"
    assert RELEASE_STATE_KEY in set(LocalMemberSource(distribution).keys())

    release = catalog.open(reference)
    assert release.release_id == artifact.pin.logical_id == reference.release_id
    manifest_profiles = ProfileRegistry.from_directory(ROOT / "profiles").list(ProfileRole.RELEASE_MANIFEST)
    assert manifest_profiles
    assert release.profiles.for_role(ProfileRole.RELEASE_MANIFEST).profile_id in {
        item.description.profile_id for item in manifest_profiles
    }

    reopened = _catalog_contract._FACTORIES[registered.description.implementation_id](
        registered,
        tmp_path / "platform",
        platform.records,
        platform.stores,
        platform.controls,
    )
    assert reopened.current() == reference
    assert reopened.open(reference) == release

    bounded = _catalog_contract._FACTORIES[registered.description.implementation_id](
        registered,
        tmp_path / "platform",
        platform.records,
        platform.stores,
        platform.controls,
    )
    bounded.max_release_bytes = 64
    with pytest.raises(LimitExceededError, match="document release"):
        bounded.open(reference)


@pytest.mark.parametrize("tamper", ("unknown-format", "extra-root-field", "changed-member", "missing-member", "extra-file"))
def test_unknown_and_incomplete_roots_are_rejected(tmp_path: Path, tamper: str) -> None:
    _, platform, committed = _committed_platform(tmp_path)
    catalog = platform.catalog
    reference = committed.reference
    distribution = (catalog.root / reference.locator).parent
    root_path = distribution / ROOT_OBJECT_KEY
    release_path = distribution / RELEASE_STATE_KEY
    root_bytes = root_path.read_bytes()
    release_bytes = release_path.read_bytes()
    extra = distribution / "extra.json"

    if tamper in {"unknown-format", "extra-root-field"}:
        import json

        root = json.loads(root_bytes)
        if tamper == "unknown-format":
            root["format"] = "unknown-artifact"
        else:
            root["extra"] = True
        root_path.write_bytes(canonical_json_bytes(root))
    elif tamper == "changed-member":
        release_path.write_bytes(release_bytes + b" ")
    elif tamper == "missing-member":
        release_path.unlink()
    else:
        extra.write_bytes(b"{}")

    try:
        with pytest.raises(IntegrityError):
            catalog.open(reference)
    finally:
        root_path.write_bytes(root_bytes)
        release_path.write_bytes(release_bytes)
        extra.unlink(missing_ok=True)
    assert catalog.current() == reference


def test_release_root_references_fail_closed_for_locator_drift_and_absence(tmp_path: Path) -> None:
    _, platform, committed = _committed_platform(tmp_path)
    catalog = platform.catalog
    reference = committed.reference

    with pytest.raises(IntegrityError, match="locator differs"):
        catalog.open(DocumentReleaseRef(reference.release_id, "document-catalog/releases/elsewhere/artifact.json", reference.digest))

    absent_digest = "sha256:" + "0" * 64
    absent = DocumentReleaseRef(
        "urn:spicy:artifact:derivation:" + "0" * 64,
        catalog._release_locator(absent_digest),
        absent_digest,
    )
    with pytest.raises((IntegrityError, ValueError)):
        catalog.open(absent)
