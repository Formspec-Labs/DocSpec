"""Run the documented contributor command with network connections forbidden."""

from __future__ import annotations

import json
import runpy
import socket
import sys
from pathlib import Path

import pytest


def test_documented_example_publishes_and_verifies_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def reject_network(*args, **kwargs):
        pytest.fail("the contributor example attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    output = tmp_path / "example"
    monkeypatch.setattr(sys, "argv", ["examples.offline_demo", "--output", str(output)])
    with pytest.raises(SystemExit) as completed:
        runpy.run_module("examples.offline_demo", run_name="__main__")
    assert completed.value.code == 0
    summary = json.loads(capfd.readouterr().out)
    assert summary == {
        "verdict": "pass",
        "recordCounts": {
            "dispositions": 1,
            "failures": 0,
            "files": 1,
            "receipts": 2,
            "representations": 1,
            "segments": 1,
            "source-items": 1,
        },
    }
    verification = json.loads((output / "verification.json").read_text())
    reference_bytes = (output / "release-reference.json").read_bytes()
    assert verification["reference"] == json.loads(reference_bytes)
    assert (output / "implementation.json").is_file()

    # Repeating the documented command refuses before changing the first run.
    with pytest.raises(SystemExit) as repeated:
        runpy.run_module("examples.offline_demo", run_name="__main__")
    assert repeated.value.code == 2
    assert (output / "release-reference.json").read_bytes() == reference_bytes
