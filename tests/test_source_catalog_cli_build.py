"""Source-native CLI composition and complete destination publication."""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

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


def test_cli_composes_the_optional_source_adapter_and_emits_a_verifiable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    receipt_path = destination / "source-catalog-build-command-receipt.json"
    arguments = source_catalog_build_arguments(
        tmp_path,
        destination=destination,
        receipt_path=receipt_path,
    )
    implementation_id = "git+https://example.test/docspec@" + "1" * 40

    assert main(arguments) == 0
    output = json.loads(capfd.readouterr().out)
    assert output["verdict"] == "pass"
    assert output["itemCount"] == 1
    assert output["catalog"] == json.loads(receipt_path.read_text())["catalog"]
    assert output["byteMeasurements"]["payloadBytesRead"] > 0
    assert output["byteMeasurements"]["payloadBytesReused"] == 0
    assert output["byteMeasurements"]["payloadBytesWritten"] > 0
    assert output["byteMeasurements"]["publicationBytesWritten"] > 0
    assert output["blobStore"] is None
    assert output["acceptedSourceVerifierImplementationIds"] == list(
        _ACCEPTED_SOURCE_VERIFIERS
    )
    assert [value["profile"] for value in output["sourceNativeInputs"]] == [
        "federal-register"
    ]

    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(output["catalog"]))
    assert (
        main(
            [
                "source-catalog",
                "verify",
                "--root",
                str(destination),
                "--reference",
                str(reference_path),
                "--expected-command-receipt-id",
                output["receiptId"],
                "--implementation-id",
                implementation_id,
                "--verifier-implementation-id",
                implementation_id,
            ]
        )
        == 0
    )
    verification = json.loads(capfd.readouterr().out)
    assert verification["commandReceiptId"] == output["receiptId"]
    assert verification["logicalId"] == output["catalog"]["catalogId"]
    assert "itemMemberPath" not in verification
    assert verification["partitions"]
    assert verification["selectionPolicy"] == output["catalogPolicy"]
    assert verification["partitionPolicy"] == output["partitionPolicy"]
    assert verification["joinCoverage"] == output["joinCoverage"]
    assert verification["diagnosticDigests"] == output["diagnosticDigests"]


def test_cli_new_destinations_reuse_verified_shared_content_blobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
    destinations = (tmp_path / "catalog-store-a", tmp_path / "catalog-store-b")
    receipts = tuple(destination / "source-catalog-build-command-receipt.json" for destination in destinations)
    blob_store = tmp_path / "catalog-blob-store"
    outputs: list[Mapping[str, Any]] = []

    for destination, receipt in zip(destinations, receipts, strict=True):
        assert (
            main(
                source_catalog_build_arguments(
                    tmp_path,
                    destination=destination,
                    receipt_path=receipt,
                    blob_store=blob_store,
                )
            )
            == 0
        )
        outputs.append(json.loads(capfd.readouterr().out))

    initial, rebuilt = outputs
    assert rebuilt["catalog"]["catalogId"] == initial["catalog"]["catalogId"]
    assert rebuilt["catalog"]["digest"] != initial["catalog"]["digest"]
    assert initial["byteMeasurements"]["payloadBytesWritten"] > 0
    assert initial["byteMeasurements"]["payloadBytesReused"] == 0
    assert rebuilt["byteMeasurements"]["payloadBytesWritten"] == 0
    assert rebuilt["byteMeasurements"]["payloadBytesReused"] > 0
    assert rebuilt["blobStore"] == {
        "path": blob_store.resolve().as_posix(),
        "retention": "verified-content-addressed-blobs-retained-for-reuse",
        "accountingStatus": "complete",
        "payloadBytesWritten": 0,
        "payloadBytesReused": rebuilt["byteMeasurements"]["payloadBytesReused"],
    }

    build_receipts = [
        json.loads(
            (
                destination / output["catalog"]["digest"].removeprefix("sha256:") / "catalog-build-receipt.json"
            ).read_text()
        )
        for destination, output in zip(destinations, outputs, strict=True)
    ]
    assert build_receipts[1]["partitions"] == build_receipts[0]["partitions"]


def test_cli_ignores_a_crash_stale_legacy_publish_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    receipt_path = destination / "source-catalog-build-command-receipt.json"
    legacy_lock = tmp_path / ".catalog-store.publish.lock"
    legacy_lock.write_text("abandoned-owner", encoding="utf-8")

    assert (
        main(
            source_catalog_build_arguments(
                tmp_path,
                destination=destination,
                receipt_path=receipt_path,
            )
        )
        == 0
    )

    output = json.loads(capfd.readouterr().out)
    assert output == json.loads(receipt_path.read_text(encoding="utf-8"))
    assert legacy_lock.read_text(encoding="utf-8") == "abandoned-owner"


def test_cli_receipt_write_failure_leaves_no_published_artifact_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    import docspec.cli.source_catalog as source_catalog_cli

    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    receipt_path = destination / "source-catalog-build-command-receipt.json"
    blob_store = tmp_path / "catalog-blob-store"
    arguments = source_catalog_build_arguments(
        tmp_path,
        destination=destination,
        receipt_path=receipt_path,
        blob_store=blob_store,
    )

    actual_write_file = source_catalog_cli.LocalSourceCatalogPublication.write_file

    def fail_success_receipt_write(
        publication: object,
        name: str,
        payload: bytes,
    ) -> None:
        if name == "source-catalog-build-command-receipt.json":
            assert not destination.exists()
            raise OSError("injected receipt write failure")
        actual_write_file(publication, name, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(
        source_catalog_cli.LocalSourceCatalogPublication,
        "write_file",
        fail_success_receipt_write,
    )

    assert main(arguments) == 2
    assert "injected receipt write failure" in capfd.readouterr().err
    assert not destination.exists()
    assert not receipt_path.exists()
    assert blob_store.is_dir()
    assert not tuple(tmp_path.glob(".catalog-store.*"))


@pytest.mark.parametrize("receipt_location", ["outside", "wrong-member"])
def test_cli_requires_the_atomic_receipt_member_without_poisoning_either_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    receipt_location: str,
) -> None:
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog"
    receipt_path = tmp_path / "receipt.json" if receipt_location == "outside" else destination / "wrong-name.json"

    assert (
        main(
            source_catalog_build_arguments(
                tmp_path,
                destination=destination,
                receipt_path=receipt_path,
            )
        )
        == 2
    )

    assert "must be the atomic artifact member" in capfd.readouterr().err
    assert not destination.exists()
    assert not receipt_path.exists()


def test_cli_rejects_source_native_containment_and_accepts_a_separate_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
    source_root = tmp_path / "source-native"

    for destination in (source_root / "catalog-store", tmp_path):
        receipt_path = destination / "source-catalog-build-command-receipt.json"
        assert (
            main(
                source_catalog_build_arguments(
                    tmp_path,
                    destination=destination,
                    receipt_path=receipt_path,
                )
            )
            == 2
        )
        assert (
            "artifact and source-native input paths must not contain one another"
            in capfd.readouterr().err
        )
        assert not receipt_path.exists()

    destination = tmp_path / "catalog-store"
    receipt_path = destination / "source-catalog-build-command-receipt.json"
    assert (
        main(
            source_catalog_build_arguments(
                tmp_path,
                destination=destination,
                receipt_path=receipt_path,
            )
        )
        == 0
    )
    output = json.loads(capfd.readouterr().out)
    assert output["destination"] == destination.resolve().as_posix()
    assert receipt_path.is_file()


def test_cli_rejects_blob_store_containment_and_receipts_explicit_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    blob_store = destination / "blobs"
    receipt_path = destination / "source-catalog-build-command-receipt.json"

    assert (
        main(
            source_catalog_build_arguments(
                tmp_path,
                destination=destination,
                receipt_path=receipt_path,
                blob_store=blob_store,
            )
        )
        == 2
    )

    assert "must not contain one another" in capfd.readouterr().err
    assert not destination.exists()
    assert not receipt_path.exists()


def test_cli_concurrent_publishers_leave_one_artifact_and_one_success_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import docspec.cli.source_catalog as source_catalog_cli

    install_fake_source_native(monkeypatch)
    monkeypatch.setattr(source_catalog_cli, "_emit", lambda *_args, **_kwargs: None)
    destinations = (tmp_path / "catalog-store", tmp_path / "catalog-store")
    receipt_paths = tuple(destination / "source-catalog-build-command-receipt.json" for destination in destinations)
    argument_sets = tuple(
        source_catalog_build_arguments(
            tmp_path,
            destination=destination,
            receipt_path=receipt_path,
        )
        for destination, receipt_path in zip(destinations, receipt_paths, strict=True)
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(main, argument_sets))

    assert sorted(results) == [0, 2]
    existing_destinations = {path for path in destinations if path.exists()}
    assert len(existing_destinations) == 1
    receipts = [json.loads(path.read_text()) for path in set(receipt_paths) if path.exists()]
    successful_receipts = [value for value in receipts if value["verdict"] == "pass"]
    assert len(successful_receipts) == 1
    receipt = successful_receipts[0]
    assert receipt["verdict"] == "pass"
    published_destination = next(iter(existing_destinations))
    assert receipt["destination"] == published_destination.resolve().as_posix()
    reference = SourceCatalogRef.from_dict(receipt["catalog"])
    summary = SourceCatalogArtifactReader(
        LocalSourceCatalogStore(published_destination, create=False),
        producer=producer(),
    ).verify_snapshot(reference)
    assert summary.item_count == 1
    assert not tuple(tmp_path.glob(".catalog-store.*"))
