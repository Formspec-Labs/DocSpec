"""Shared source-native CLI setup and build arguments."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from rulespec_artifacts import ArtifactPin, LocalBlobSource

from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.identity import canonical_json_file_bytes
from tests.support.source_catalog import _FEDERAL_REGISTER_SOURCE, _SHA_A, description, record, renditions

_ACCEPTED_SOURCE_VERIFIERS = (
    "urn:test:source-verifier:sha256:" + "8" * 64,
    "urn:test:source-verifier:sha256:" + "9" * 64,
)


def install_fake_source_native(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeReader:
        def __init__(
            self,
            source,
            *,
            blob_source: object,
            profile: object,
            accepted_verifier_implementation_ids: frozenset[str],
            expected_pin: ArtifactPin | None = None,
        ) -> None:
            assert source is not None
            assert isinstance(blob_source, LocalBlobSource)
            assert profile is fake_profile
            assert expected_pin is None
            assert accepted_verifier_implementation_ids == frozenset(
                _ACCEPTED_SOURCE_VERIFIERS
            )
            self.pin = ArtifactPin(description().logical_id, description().artifact_digest)
            self.source_state_scope = description().source_state_scope
            self.source_system_id = description().source_system_id
            self.source_system_version = description().source_system_version
            self.source_state_digest = description().source_state_digest
            self.source_native_schema_set_digest = description().source_native_schema_set_digest
            self.collection_outcome = {
                "recordOutcome": "no-record-rejections", "sourceStateScope": self.source_state_scope,
                "publishedRecordCount": 1, "failedRecordCount": 0,
                "requestedScope": {"providerQuery": {"page": 1}},
            }

        def record_evidence(self, source_record_id):
            return None

        def iter_failures(self, *, limit=100):
            yield from ()

        def read_evidence(self, blob_ref, *, max_bytes):
            raise ValueError("fixture has no provider evidence blobs")

        def iter_records(self):
            yield record("2026-00001")

        def iter_renditions(self):
            yield from renditions("2026-00001")

    fake_profile = object()

    # Shadow the producer package the adapter resolves first, so these tests
    # exercise the fake reader whether or not a real one is installed.
    package_name = "spicy_docs"
    module_name = package_name + ".source_native"
    profiles_module_name = package_name + ".source_native_profiles"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    module = ModuleType(module_name)
    profiles_module = ModuleType(profiles_module_name)
    module.CURRENT_PRODUCER_PRODUCT = "spicy-docs"  # type: ignore[attr-defined]
    module.SourceNativeReleaseReader = FakeReader  # type: ignore[attr-defined]
    profiles_module.FEDERAL_REGISTER_PROFILE = fake_profile  # type: ignore[attr-defined]
    profiles_module.REGULATIONS_GOV_DOCUMENT_PROFILE = object()  # type: ignore[attr-defined]
    profiles_module.REGULATIONS_GOV_DOCKET_PROFILE = object()  # type: ignore[attr-defined]
    profiles_module.REGULATIONS_GOV_COMMENT_PROFILE = object()  # type: ignore[attr-defined]
    package.source_native = module  # type: ignore[attr-defined]
    package.source_native_profiles = profiles_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setitem(sys.modules, profiles_module_name, profiles_module)


def source_catalog_build_arguments(
    tmp_path: Path,
    *,
    destination: Path,
    blob_store: Path | None = None,
) -> list[str]:
    source_root = tmp_path / "source-native"
    source_root.mkdir(exist_ok=True)
    source_blob_store = tmp_path / "source-native-blobs"
    source_blob_store.mkdir(exist_ok=True)
    policy_path = tmp_path / "catalog-policy.json"
    policy_path.write_bytes(
        canonical_json_file_bytes(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).to_member())
    )
    implementation_id = "git+https://example.test/docspec@" + "1" * 40
    arguments = [
        "source-catalog",
        "build",
        "--source-native",
        str(source_root),
        "--source-native-artifact-digest",
        _SHA_A,
        "--source-native-blob-store",
        str(source_blob_store),
        "--source-native-profile",
        "federal-register",
        "--accepted-source-verifier-implementation-id",
        _ACCEPTED_SOURCE_VERIFIERS[1],
        "--accepted-source-verifier-implementation-id",
        _ACCEPTED_SOURCE_VERIFIERS[0],
        "--catalog-policy",
        str(policy_path),
        "--implementation-id",
        implementation_id,
        "--verifier-implementation-id",
        implementation_id,
        "--destination",
        str(destination),
    ]
    if blob_store is not None:
        arguments.extend(("--blob-store", str(blob_store)))
    return arguments


def source_catalog_verify_arguments(destination: Path, reference_path: Path) -> list[str]:
    implementation_id = "git+https://example.test/docspec@" + "1" * 40
    return [
        "source-catalog", "verify", "--root", str(destination), "--reference", str(reference_path),
        "--implementation-id", implementation_id, "--verifier-implementation-id", implementation_id,
    ]
