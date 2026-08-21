from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from docspec.domain.identity import canonical_json_file_bytes, parse_canonical_json, sha256_digest, thaw_json
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import DocumentReleaseRef
from docspec.domain.release import DocumentRelease
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

    root_path = catalog.root / reference.locator
    payload = root_path.read_bytes()
    value = thaw_json(parse_canonical_json(payload, label="published release root"))
    assert isinstance(value, dict)
    assert payload == canonical_json_file_bytes(value), "the published root must be canonical JSON"
    assert sha256_digest(payload) == reference.digest
    release = DocumentRelease.from_dict(value)
    assert release.file_bytes == payload
    assert release.release_id == reference.release_id

    manifest_profiles = ProfileRegistry.from_directory(ROOT / "profiles").list(ProfileRole.RELEASE_MANIFEST)
    assert manifest_profiles
    manifest_ids = {item.description.profile_id for item in manifest_profiles}
    assert release.profiles.for_role(ProfileRole.RELEASE_MANIFEST).profile_id in manifest_ids
    assert all(
        isinstance(item.description.limits["maxRootBytes"], int) and item.description.limits["maxRootBytes"] > 0
        for item in manifest_profiles
    )

    # A second catalog instance over the same storage state must find and
    # verify the head purely from the published files.
    reopened = _catalog_contract._FACTORIES[registered.description.implementation_id](
        registered,
        tmp_path / "platform",
        platform.records,
        platform.stores,
        platform.controls,
    )
    assert reopened.current() == reference
    assert reopened.open(reference) == release

    pointer = thaw_json(
        parse_canonical_json(
            (catalog.root / "document-catalog" / "current.json").read_bytes(),
            label="current pointer",
        )
    )
    assert pointer == {
        "format": "docspec-document-catalog-current",
        "formatVersion": "1.0",
        "release": reference.to_dict(),
    }

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


@pytest.mark.parametrize(
    ("tamper", "expected_message"),
    [
        ("unknown-format", "document release is invalid"),
        ("unknown-format-version", "document release is invalid"),
        ("incomplete-root", "document release is invalid"),
        ("extra-member", "document release is invalid"),
        ("changed-content-under-identity", "identity differs"),
    ],
)
def test_unknown_and_incomplete_roots_are_rejected(
    tmp_path: Path,
    tamper: str,
    expected_message: str,
) -> None:
    _, platform, committed = _committed_platform(tmp_path)
    catalog = platform.catalog
    reference = committed.reference
    root_path = catalog.root / reference.locator
    original = root_path.read_bytes()
    value: dict[str, Any] = thaw_json(parse_canonical_json(original, label="published release root"))

    mutate: Callable[[dict[str, Any]], None] = {
        "unknown-format": lambda root: root.update(format="docspec-unknown-release"),
        "unknown-format-version": lambda root: root.update(formatVersion="9.9"),
        "incomplete-root": lambda root: root.pop("activeLayers"),
        "extra-member": lambda root: root.update(undeclaredMember=True),
        "changed-content-under-identity": lambda root: root["counts"].update(
            activeLayers=root["counts"]["activeLayers"] + 1
        ),
    }[tamper]
    mutate(value)
    tampered = canonical_json_file_bytes(value)
    tampered_reference = DocumentReleaseRef(
        reference.release_id,
        reference.locator,
        sha256_digest(tampered),
    )
    try:
        root_path.write_bytes(tampered)
        with pytest.raises(IntegrityError, match=expected_message):
            catalog.open(tampered_reference)
        with pytest.raises(IntegrityError, match="bytes differ"):
            catalog.open(reference)
    finally:
        root_path.write_bytes(original)
    assert catalog.current() == reference, "a rejected root must not disturb the committed head"


def test_release_root_references_fail_closed_for_locator_drift_and_absence(tmp_path: Path) -> None:
    _, platform, committed = _committed_platform(tmp_path)
    catalog = platform.catalog
    reference = committed.reference

    with pytest.raises(IntegrityError, match="locator differs from its identity"):
        catalog.open(
            DocumentReleaseRef(
                reference.release_id,
                "document-catalog/releases/aa/somewhere-else.json",
                reference.digest,
            )
        )

    absent_id = "urn:docspec:document-release:v1:absent"
    absent_key = hashlib.sha256(absent_id.encode("utf-8")).hexdigest()
    absent_locator = f"document-catalog/releases/{absent_key[:2]}/{absent_key}.json"
    with pytest.raises(IntegrityError):
        catalog.open(DocumentReleaseRef(absent_id, absent_locator, reference.digest))
