"""Source-native CLI build contract: the optional source adapter is composed over local blob roots, a successful
build publishes a verifiable snapshot and emits a pass report, and any failure leaves no destination, staging
directory or success report.

Covers pinning the source blob root across streams (a changed root refuses instead of rereading), shared
verified-content-addressed blobs reused across new destinations, containment refusals for source/destination and
destination/blob-store overlaps, and concurrent publishers leaving exactly one verified catalog.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest
from rulespec_artifacts import ArtifactPin, LocalBlobSource, MemberSourceError

from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.adapters.spicy_docs_source_native import SpicyDocsSourceNativeAdapter
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.references import SourceCatalogRef
from docspec.entrypoint import main
from tests.support.source_catalog import description, producer
from tests.support.source_catalog_cli import (
    _ACCEPTED_SOURCE_VERIFIERS,
    install_fake_source_native,
    source_catalog_build_arguments,
    source_catalog_verify_arguments,
)


def test_spicy_docs_adapter_pins_the_source_blob_root_across_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"original source bytes\n"
    blob_ref = sha256_digest(payload)
    source_root = tmp_path / "source"
    blob_root = tmp_path / "blobs"
    source_root.mkdir()
    blob = blob_root / "sha256" / blob_ref.removeprefix("sha256:")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    observed: list[bytes] = []

    class FakeReader:
        """Minimal source-native reader double that reads every blob through the supplied LocalBlobSource."""

        def __init__(
            self,
            source: object,
            *,
            blob_source: object,
            profile: object,
            accepted_verifier_implementation_ids: frozenset[str],
            expected_pin: ArtifactPin | None = None,
        ) -> None:
            del source, profile, accepted_verifier_implementation_ids
            assert expected_pin is None
            assert isinstance(blob_source, LocalBlobSource)
            self._blob_source = blob_source
            self.pin = ArtifactPin(description().logical_id, description().artifact_digest)
            self.source_state_scope = description().source_state_scope
            self.source_system_id = description().source_system_id
            self.source_system_version = description().source_system_version
            self.source_state_digest = description().source_state_digest
            self.source_native_schema_set_digest = description().source_native_schema_set_digest
            self.collection_outcome = {"recordOutcome": "empty", "sourceStateScope": self.source_state_scope}

        def record_evidence(self, source_record_id):
            return None

        def iter_failures(self, *, limit=100):
            return iter(())

        def read_evidence(self, reference, *, max_bytes):
            with self._blob_source.open(reference) as stream:
                value = stream.read(max_bytes + 1)
            if len(value) > max_bytes:
                raise ValueError("fixture evidence exceeds its byte limit")
            return value

        def iter_records(self):
            with self._blob_source.open(blob_ref) as stream:
                observed.append(stream.read())
            return iter(())

        def iter_renditions(self):
            return iter(())

    module_name = "spicy_docs.source_native"
    source_native = ModuleType(module_name)
    source_native.CURRENT_PRODUCER_PRODUCT = "spicy-docs"  # type: ignore[attr-defined]
    source_native.SourceNativeReleaseReader = FakeReader  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "docspec.adapters.spicy_docs_source_native.import_module",
        lambda name: source_native
        if name == module_name
        else (_ for _ in ()).throw(ModuleNotFoundError(name, name=name)),
    )
    adapter = SpicyDocsSourceNativeAdapter.from_local(
        source_root,
        blob_root=blob_root,
        artifact_digest=description().artifact_digest,
        profile=object(),
        accepted_verifier_implementation_ids=frozenset({"urn:test:verifier"}),
    )

    retained = tmp_path / "retained-blobs"
    blob_root.rename(retained)
    replacement = blob_root / "sha256" / blob_ref.removeprefix("sha256:")
    replacement.parent.mkdir(parents=True)
    replacement.write_bytes(b"changed source bytes!\n")

    with pytest.raises(MemberSourceError, match="artifact root changed"):
        tuple(adapter.iter_records())
    assert observed == []
    assert replacement.read_bytes() == b"changed source bytes!\n"


def test_cli_composes_the_optional_source_adapter_and_emits_a_verifiable_snapshot(tmp_path, monkeypatch, capfd):
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    assert main(source_catalog_build_arguments(tmp_path, destination=destination)) == 0
    output = json.loads(capfd.readouterr().out)
    assert output["format"] == "docspec-source-catalog-build-report"
    assert output["verdict"] == "pass" and output["itemCount"] == 1
    assert not (destination / "source-catalog-build-command-receipt.json").exists()
    assert output["byteMeasurements"]["payloadBytesRead"] > 0
    assert output["byteMeasurements"]["payloadBytesReused"] == 0
    assert output["byteMeasurements"]["payloadBytesWritten"] > 0
    assert output["byteMeasurements"]["publicationBytesWritten"] > 0
    assert output["blobStore"] is None
    assert output["acceptedSourceVerifierImplementationIds"] == list(_ACCEPTED_SOURCE_VERIFIERS)
    assert [value["profile"] for value in output["sourceNativeInputs"]] == ["federal-register"]

    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(output["catalog"]))
    assert main(source_catalog_verify_arguments(destination, reference_path)) == 0
    verification = json.loads(capfd.readouterr().out)
    assert verification["formatVersion"] == "2.0"
    assert verification["logicalId"] == output["catalog"]["catalogId"]
    assert verification["artifactDigest"] == output["catalog"]["digest"]
    assert verification["partitions"]
    assert verification["selectionPolicy"] == output["catalogPolicy"]
    for field in ("partitionPolicy", "joinCoverage", "diagnosticDigests", "byteMeasurements"):
        assert verification[field] == output[field]


def test_cli_new_destinations_reuse_verified_shared_content_blobs(tmp_path, monkeypatch, capfd):
    install_fake_source_native(monkeypatch)
    destinations = (tmp_path / "catalog-store-a", tmp_path / "catalog-store-b")
    blob_store = tmp_path / "catalog-blob-store"
    outputs = []
    for destination in destinations:
        assert main(source_catalog_build_arguments(tmp_path, destination=destination, blob_store=blob_store)) == 0
        outputs.append(json.loads(capfd.readouterr().out))
    initial, rebuilt = outputs
    assert rebuilt["catalog"]["catalogId"] == initial["catalog"]["catalogId"]
    assert rebuilt["catalog"]["digest"] != initial["catalog"]["digest"]
    assert initial["byteMeasurements"]["payloadBytesWritten"] > 0
    assert initial["byteMeasurements"]["payloadBytesReused"] == 0
    assert rebuilt["byteMeasurements"]["payloadBytesWritten"] == 0
    assert rebuilt["byteMeasurements"]["payloadBytesReused"] > 0
    assert rebuilt["blobStore"] == {
        "path": blob_store.resolve().as_posix(), "retention": "verified-content-addressed-blobs-retained-for-reuse",
        "accountingStatus": "complete", "payloadBytesWritten": 0,
        "payloadBytesReused": rebuilt["byteMeasurements"]["payloadBytesReused"],
    }
    receipts = [json.loads((destination / output["catalog"]["digest"].removeprefix("sha256:") /
        "catalog-build-receipt.json").read_bytes()) for destination, output in zip(destinations, outputs, strict=True)]
    assert receipts[1]["partitions"] == receipts[0]["partitions"]


def test_cli_publication_failure_leaves_no_destination_or_success_report(tmp_path, monkeypatch, capfd):
    import docspec.cli.source_catalog as source_catalog_cli

    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    blob_store = tmp_path / "catalog-blob-store"

    def fail_publication(publication):
        assert publication.root.is_dir() and not destination.exists()
        raise OSError("injected publication failure")

    monkeypatch.setattr(source_catalog_cli.LocalSourceCatalogPublication, "publish", fail_publication)
    assert main(source_catalog_build_arguments(tmp_path, destination=destination, blob_store=blob_store)) == 2
    captured = capfd.readouterr()
    assert captured.out == "" and "injected publication failure" in captured.err
    assert not destination.exists()
    assert blob_store.is_dir()
    assert not tuple(tmp_path.glob(".catalog-store.*"))


def test_cli_rejects_source_native_containment_and_accepts_a_separate_destination(tmp_path, monkeypatch, capfd):
    install_fake_source_native(monkeypatch)
    source_root = tmp_path / "source-native"
    for destination in (source_root / "catalog-store", tmp_path):
        arguments = source_catalog_build_arguments(tmp_path, destination=destination)
        before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}
        assert main(arguments) == 2
        assert "artifact and source-native input paths must not contain one another" in capfd.readouterr().err
        assert {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")} == before
    destination = tmp_path / "catalog-store"
    assert main(source_catalog_build_arguments(tmp_path, destination=destination)) == 0
    output = json.loads(capfd.readouterr().out)
    assert output["destination"] == destination.resolve().as_posix()
    assert (destination / output["catalog"]["digest"].removeprefix("sha256:") / "artifact.json").is_file()


def test_cli_rejects_blob_store_containment(tmp_path, monkeypatch, capfd):
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    assert main(source_catalog_build_arguments(tmp_path, destination=destination, blob_store=destination / "blobs")) == 2
    assert "must not contain one another" in capfd.readouterr().err
    assert not destination.exists()


def test_cli_concurrent_publishers_leave_one_verified_catalog(tmp_path, monkeypatch):
    import docspec.cli.source_catalog as source_catalog_cli

    install_fake_source_native(monkeypatch)
    reports = []
    monkeypatch.setattr(source_catalog_cli, "_emit", lambda value, **_kwargs: reports.append(value))
    destination = tmp_path / "catalog-store"
    argument_sets = tuple(source_catalog_build_arguments(tmp_path, destination=destination) for _ in range(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(main, argument_sets))
    assert sorted(results) == [0, 2]
    report, = (value for value in reports if value["format"] == "docspec-source-catalog-build-report")
    assert report["verdict"] == "pass" and report["destination"] == destination.resolve().as_posix()
    summary = SourceCatalogArtifactReader(LocalSourceCatalogStore(destination, create=False), producer=producer()
        ).verify_snapshot(SourceCatalogRef.from_dict(report["catalog"]))
    assert summary.item_count == 1
    assert not tuple(tmp_path.glob(".catalog-store.*"))
