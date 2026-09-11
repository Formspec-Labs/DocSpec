"""Retain an experiment independently of the selected dataset result."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from docspec.cli import main
from docspec.cli.execution import run_local
from docspec.cli.requests import _local_storage_for_run_request
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import DocumentReleaseRef
from docspec.profile_registry import ProfileRegistry
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


def _write(path: Path, value: dict) -> Path:
    path.write_bytes(canonical_json_file_bytes(value))
    return path


def _operation(tmp_path: Path, capfd, name: str, group: str, command: str, value: dict, *, expected: int = 0):
    request = _write(tmp_path / f"{name}-request.json", value)
    destination = tmp_path / f"{name}-result.json"
    receipt = tmp_path / f"{name}-receipt.json"
    assert main([
        group, command, "--request", str(request),
        "--destination", str(destination), "--receipt", str(receipt),
    ]) == expected
    captured = capfd.readouterr()
    if expected:
        assert not destination.exists()
        assert json.loads(captured.err)["verdict"] == "fail"
        return None
    assert json.loads(captured.out)["operation"] == f"{group}.{command}"
    return DocumentReleaseRef.from_dict(json.loads(destination.read_bytes()))


def test_cli_retains_alternatives_then_selects_with_explicit_current(tmp_path: Path, capfd) -> None:
    run_request, roots = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    fetcher = SharedFixtureContentFetcher(Path(roots["sourceContent"]))

    def retain(name: str) -> DocumentReleaseRef:
        run = run_local(run_request, resume=False, content_fetcher=fetcher)
        run_receipt = _write(tmp_path / f"{name}-run.json", run.to_dict())
        return _operation(tmp_path, capfd, name, "document-release", "retain", {
            "format": "docspec-local-release-retain-request", "formatVersion": "1.0",
            "runRequest": str(run_request), "runReceipt": str(run_receipt), "baseRelease": None,
        })

    first = retain("first")
    *_, catalog = _local_storage_for_run_request(run_request)
    assert catalog.current() is None
    assert catalog.open(first).previous_release is None

    def select(name: str, candidate: DocumentReleaseRef, expected_current, *, expected: int = 0):
        return _operation(tmp_path, capfd, name, "document-catalog", "select", {
            "format": "docspec-local-catalog-select-request", "formatVersion": "1.0",
            "runRequest": str(run_request), "release": candidate.to_dict(),
            "expectedCurrent": None if expected_current is None else expected_current.to_dict(),
        }, expected=expected)

    assert select("choose-first", first, None) == first
    # Change an actual work setting, keeping the same source and base. The
    # second result must be retained without first changing current selection.
    plan_path = Path(json.loads(run_request.read_bytes())["plan"])
    plan = ProcessingPlan.from_dict(json.loads(plan_path.read_bytes()))
    plan_arguments = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    plan_arguments["limits"] = replace(plan.limits, max_segments=plan.limits.max_segments + 1)
    _write(plan_path, ProcessingPlan.create(**plan_arguments).to_dict())
    second = retain("second")
    assert second != first
    assert catalog.current() == first
    assert catalog.open(second).previous_release is None
    assert select("choose-second", second, first) == second
    assert catalog.current() == second
    # A stale caller cannot replace the user's latest explicit choice.
    select("stale-choice", first, None, expected=2)
    assert catalog.current() == second
    assert catalog.open(first).previous_release is None


@pytest.mark.parametrize("change", [
    {"formatVersion": "unknown"},
    {"extra": True},
    {"runRequest": "relative.json"},
])
def test_bad_selection_requests_refuse_before_opening_runtime(tmp_path: Path, capfd, change: dict) -> None:
    value = {
        "format": "docspec-local-catalog-select-request", "formatVersion": "1.0",
        "runRequest": str(tmp_path / "missing-run.json"), "release": {}, "expectedCurrent": None,
    } | change
    _operation(tmp_path, capfd, "bad", "document-catalog", "select", value, expected=2)
    assert not (tmp_path / "documentCatalog").exists()
