"""Source-catalog CLI verify contract: verification uses the pinned catalog's existing bytes and semantic
checks, passing after the store is moved and never repairing it.

Refuses a different catalog pin, an unaccepted producer and changed member bytes, writing a fail JSON to stderr
and leaving the destination untouched.
"""

from __future__ import annotations

import json

import pytest

from docspec.domain.identity import canonical_json_file_bytes
from docspec.entrypoint import main
from tests.support.source_catalog import FakeSource, description, record, renditions
from tests.support.source_catalog_builds import build
from tests.support.source_catalog_cli import (
    install_fake_source_native, source_catalog_build_arguments, source_catalog_verify_arguments,
)


@pytest.fixture
def published_catalog(tmp_path, monkeypatch, capfd):
    """Build a catalog through the CLI and return its destination, pinned-reference path and catalog dict."""
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    assert main(source_catalog_build_arguments(tmp_path, destination=destination)) == 0
    report = json.loads(capfd.readouterr().out)
    reference_path = tmp_path / "reference.json"
    reference_path.write_bytes(canonical_json_file_bytes(report["catalog"]))
    return destination, reference_path, report["catalog"]


def test_cli_verifies_a_catalog_without_its_original_build_location(published_catalog, tmp_path, capfd):
    destination, reference_path, reference = published_catalog
    moved = tmp_path / "moved-catalog"
    destination.rename(moved)
    before = {path: path.stat().st_mtime_ns for path in moved.rglob("*")}
    assert main(source_catalog_verify_arguments(moved, reference_path)) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["logicalId"] == reference["catalogId"] and result["artifactDigest"] == reference["digest"]
    assert result["verdict"] == "pass" and result["itemCount"] == 1
    assert {path: path.stat().st_mtime_ns for path in moved.rglob("*")} == before


def test_cli_verifies_a_catalog_built_without_the_cli(tmp_path, capfd):
    destination = tmp_path / "catalog-store"
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    _, result = build(destination, source)
    reference_path = tmp_path / "reference.json"
    reference_path.write_bytes(canonical_json_file_bytes(result.reference.to_dict()))
    assert main(source_catalog_verify_arguments(destination, reference_path)) == 0
    verification = json.loads(capfd.readouterr().out)
    assert verification["verdict"] == "pass" and verification["itemCount"] == 1
    assert verification["artifactDigest"] == result.reference.digest


@pytest.mark.parametrize("changed_pin", ["catalogId", "digest"])
def test_cli_verify_refuses_a_different_catalog_pin(published_catalog, capfd, changed_pin):
    destination, reference_path, reference = published_catalog
    reference[changed_pin] = "urn:test:different-catalog" if changed_pin == "catalogId" else "sha256:" + "f" * 64
    reference_path.write_bytes(canonical_json_file_bytes(reference))
    assert main(source_catalog_verify_arguments(destination, reference_path)) == 2
    captured = capfd.readouterr()
    assert captured.out == "" and json.loads(captured.err)["verdict"] == "fail"


def test_cli_verify_refuses_an_unaccepted_producer(published_catalog, capfd):
    destination, reference_path, _ = published_catalog
    arguments = source_catalog_verify_arguments(destination, reference_path)
    arguments[arguments.index("--verifier-implementation-id") + 1] = "urn:test:different-verifier"
    assert main(arguments) == 2
    captured = capfd.readouterr()
    assert captured.out == "" and json.loads(captured.err)["verdict"] == "fail"


@pytest.mark.parametrize("member", ["artifact.json", "catalog-build-receipt.json"])
def test_cli_verify_refuses_changed_catalog_bytes_without_repairing_them(published_catalog, capfd, member):
    destination, reference_path, reference = published_catalog
    path = destination / reference["digest"].removeprefix("sha256:") / member
    changed = path.read_bytes() + b" "
    path.write_bytes(changed)
    before = {path: path.stat().st_mtime_ns for path in destination.rglob("*")}
    assert main(source_catalog_verify_arguments(destination, reference_path)) == 2
    captured = capfd.readouterr()
    assert captured.out == "" and json.loads(captured.err)["verdict"] == "fail"
    assert path.read_bytes() == changed
    assert {path: path.stat().st_mtime_ns for path in destination.rglob("*")} == before
