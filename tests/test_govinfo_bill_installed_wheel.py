"""Qualify the bill example with installed packages outside either checkout."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ROOT = ROOT / "vendor"


def test_bill_example_uses_installed_provider_and_reprocesses_offline(tmp_path, docspec_wheel):
    uv = shutil.which("uv")
    assert uv, "the installed-wheel bill qualification requires uv"
    manifest = json.loads((PROVIDER_ROOT / "spicy_docs.json").read_text())
    provider = PROVIDER_ROOT / manifest["filename"]
    assert hashlib.sha256(provider.read_bytes()).hexdigest() == manifest["sha256"]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
    environment["PYTHONNOUSERSITE"] = "1"

    def run(arguments):
        result = subprocess.run([str(value) for value in arguments], cwd=tmp_path, env=environment,
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr

    wheels = tmp_path / "wheelhouse"
    wheels.mkdir()
    docspec = Path(shutil.copy2(docspec_wheel, wheels / docspec_wheel.name))
    with zipfile.ZipFile(docspec) as archive:
        assert not any(name.startswith("spicy_docs/") or name.endswith(".whl") for name in archive.namelist())
    spicy_docs = Path(shutil.copy2(provider, wheels / provider.name))
    rulespec = Path(shutil.copy2(ROOT / "vendor/rulespec_artifacts-1.0.12-py3-none-any.whl", wheels))
    venv = tmp_path / "environment"
    run([uv, "venv", "--python", sys.executable, venv])
    python = venv / "bin/python"
    run([uv, "pip", "install", "--python", python, rulespec, str(docspec) + "[dagster]",
         str(spicy_docs) + "[acquisition]"])
    run([uv, "pip", "check", "--python", python])
    examples = tmp_path / "examples"
    examples.mkdir()
    for filename in (
        "__init__.py", "govinfo_bills.py", "govinfo_bill_fetcher.py", "phrase_match_processor.py", "provider_identity.py",
    ):
        shutil.copy2(ROOT / "examples" / filename, examples / filename)
    shutil.copytree(ROOT / "examples/bill_fixtures", examples / "bill_fixtures")
    shutil.copy2(ROOT / "tests/support/govinfo_bill_probe.py", examples / "govinfo_bill_probe.py")
    proof = tmp_path / "proof.json"
    run([python, "-m", "examples.govinfo_bill_probe", tmp_path, proof, spicy_docs, manifest["sha256"]])
    result = json.loads(proof.read_text())
    assert result["verdict"] == "pass"
    assert result["providerVersion"] == manifest["version"]
    assert result["providerWheelSha256"] == manifest["sha256"]
    assert result["firstMatches"] == 2 and result["laterMatches"] == 3
    assert result["xmlCapturedBytes"] == (ROOT / "examples/bill_fixtures/introduced.xml").stat().st_size
    assert result["freshCheckoutImports"] and result["networkForbidden"]
    assert result["providerTextCalls"] == 1 and result["closedBeforeReprocessing"]
    assert result["unavailableSelectionPreservesStatus"] and result["refusedTextRetainsBody"]
