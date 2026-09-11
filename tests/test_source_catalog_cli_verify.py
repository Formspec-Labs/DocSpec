"""Closed command receipts and explicit source-catalog CLI admission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docspec.domain.identity import canonical_json_file_bytes, stable_urn
from docspec.entrypoint import main
from tests.support.source_catalog_cli import (
    install_fake_source_native,
    source_catalog_build_arguments,
)


def _verify_source_catalog_arguments(
    destination: Path,
    reference_path: Path,
    expected_command_receipt_id: str,
) -> list[str]:
    implementation_id = "git+https://example.test/docspec@" + "1" * 40
    return [
        "source-catalog",
        "verify",
        "--root",
        str(destination),
        "--reference",
        str(reference_path),
        "--expected-command-receipt-id",
        expected_command_receipt_id,
        "--implementation-id",
        implementation_id,
        "--verifier-implementation-id",
        implementation_id,
    ]


def _rewrite_command_receipt(
    receipt_path: Path,
    receipt: dict[str, Any],
    *,
    recompute_id: bool,
) -> None:
    if recompute_id:
        content = {
            key: value
            for key, value in receipt.items()
            if key not in {"format", "formatVersion", "receiptId"}
        }
        receipt["receiptId"] = stable_urn(
            "source-catalog-build-command-receipt",
            content,
        )
    receipt_path.write_bytes(canonical_json_file_bytes(receipt))


@pytest.mark.parametrize("change", ["missing", "unknown-field"])
def test_cli_verify_requires_one_closed_build_command_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    change: str,
) -> None:
    install_fake_source_native(monkeypatch)
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
    command_receipt = json.loads(capfd.readouterr().out)
    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(command_receipt["catalog"]))

    if change == "missing":
        receipt_path.unlink()
    else:
        command_receipt["unknown"] = True
        _rewrite_command_receipt(receipt_path, command_receipt, recompute_id=True)

    assert (
        main(
            _verify_source_catalog_arguments(
                destination,
                reference_path,
                command_receipt["receiptId"],
            )
        )
        == 2
    )
    error = capfd.readouterr().err
    if change == "missing":
        assert "must be a regular, non-symlink file" in error
    else:
        assert "invalid closed shape" in error


@pytest.mark.parametrize(
    ("changed_fact", "expected_label"),
    [
        ("catalog-state", "catalogStateDigest"),
        ("source-logical-id", "sourceNativeInputs"),
        ("source-artifact-digest", "sourceNativeInputs"),
        ("byte-measurements", "byteMeasurements"),
    ],
)
def test_cli_verify_rejects_a_self_consistent_command_summary_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    changed_fact: str,
    expected_label: str,
) -> None:
    install_fake_source_native(monkeypatch)
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
    command_receipt = json.loads(capfd.readouterr().out)
    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(command_receipt["catalog"]))
    if changed_fact == "catalog-state":
        command_receipt["catalogStateDigest"] = "sha256:" + "f" * 64
    elif changed_fact == "source-logical-id":
        command_receipt["sourceNativeInputs"][0]["logicalId"] = "urn:test:different-source"
    elif changed_fact == "source-artifact-digest":
        command_receipt["sourceNativeInputs"][0]["artifactDigest"] = "sha256:" + "f" * 64
    else:
        command_receipt["byteMeasurements"]["payloadBytesRead"] += 1
        command_receipt["byteMeasurements"]["payloadBytesWritten"] += 1
    _rewrite_command_receipt(receipt_path, command_receipt, recompute_id=True)

    assert (
        main(
            _verify_source_catalog_arguments(
                destination,
                reference_path,
                command_receipt["receiptId"],
            )
        )
        == 2
    )
    assert f"{expected_label} differs from the admitted catalog" in capfd.readouterr().err


def test_cli_verify_requires_the_expected_command_receipt_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
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
    command_receipt = json.loads(capfd.readouterr().out)
    expected_receipt_id = command_receipt["receiptId"]
    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(command_receipt["catalog"]))
    command_receipt["acceptedSourceVerifierImplementationIds"] = [
        "urn:test:different-source-verifier"
    ]
    _rewrite_command_receipt(receipt_path, command_receipt, recompute_id=True)

    assert (
        main(
            _verify_source_catalog_arguments(
                destination,
                reference_path,
                expected_receipt_id,
            )
        )
        == 2
    )
    assert "differs from the expected receipt identity" in capfd.readouterr().err


def test_cli_verify_binds_the_selected_source_profile_to_the_receipt_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
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
    command_receipt = json.loads(capfd.readouterr().out)
    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(command_receipt["catalog"]))
    command_receipt["sourceNativeInputs"][0]["profile"] = "regulations-gov-documents"
    _rewrite_command_receipt(receipt_path, command_receipt, recompute_id=False)

    assert (
        main(
            _verify_source_catalog_arguments(
                destination,
                reference_path,
                command_receipt["receiptId"],
            )
        )
        == 2
    )
    assert "receiptId does not match its content" in capfd.readouterr().err

    command_receipt["sourceNativeInputs"][0]["profile"] = "unregistered"
    _rewrite_command_receipt(receipt_path, command_receipt, recompute_id=True)
    assert (
        main(
            _verify_source_catalog_arguments(
                destination,
                reference_path,
                command_receipt["receiptId"],
            )
        )
        == 2
    )
    assert "unsupported source-native profile" in capfd.readouterr().err


@pytest.mark.parametrize("changed_pin", ["destination", "reference"])
def test_cli_verify_binds_the_command_receipt_to_the_explicit_admission_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    changed_pin: str,
) -> None:
    install_fake_source_native(monkeypatch)
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
    command_receipt = json.loads(capfd.readouterr().out)
    reference = dict(command_receipt["catalog"])
    reference_path = tmp_path / "source-catalog-ref.json"
    if changed_pin == "destination":
        moved_destination = tmp_path / "moved-catalog-store"
        destination.rename(moved_destination)
        destination = moved_destination
    else:
        reference["catalogId"] = "urn:test:different-catalog"
    reference_path.write_bytes(canonical_json_file_bytes(reference))

    assert (
        main(
            _verify_source_catalog_arguments(
                destination,
                reference_path,
                command_receipt["receiptId"],
            )
        )
        == 2
    )
    error = capfd.readouterr().err
    if changed_pin == "destination":
        assert "destination differs from the explicit store root" in error
    else:
        assert "reference differs from the published build command receipt" in error
