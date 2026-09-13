"""Qualify the native DI example and interruption through installed packages."""

import json
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_installed_native_dagster_resources_recover_document_checkpoints(tmp_path, docspec_wheel):
    pytest.importorskip("dagster")
    uv = shutil.which("uv")
    assert uv is not None

    def command(arguments, *, timeout=120):
        result = subprocess.run(arguments, cwd=tmp_path, capture_output=True, text=True, timeout=timeout, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    environment = tmp_path / "environment"
    command([uv, "venv", "--python", sys.executable, str(environment)])
    python = environment / "bin/python"
    command([uv, "pip", "install", "--python", str(python), str(docspec_wheel),
             str(ROOT / "vendor/rulespec_artifacts-1.0.12-py3-none-any.whl"), f"dagster=={version('dagster')}"])
    examples = tmp_path / "examples"
    examples.mkdir()
    for name in ("offline_demo.py", "phrase_match_processor.py", "dagster_experiment.py"):
        shutil.copy2(ROOT / "examples" / name, examples / name)
    shutil.copytree(ROOT / "examples/offline", examples / "offline")
    shutil.copy2(ROOT / "tests/support/dagster_experiment_probe.py", tmp_path / "dagster_experiment_probe.py")
    # The copied probe and examples are the only additions to this isolated
    # interpreter. Workers reconstruct from that directory, not the checkout.
    result = command([python, "-I", "-c", "import pathlib, sys; "
                      "sys.path.insert(0, str(pathlib.Path.cwd())); "
                      "import docspec; assert pathlib.Path(docspec.__file__).is_relative_to(sys.prefix); "
                      "from dagster_experiment_probe import main; main()"], timeout=240)
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["installedNativeExperiment"] == "pass"
    assert summary["sourceRefusalPreserved"]
    assert summary["capturesRepeated"] == 0 and not summary["completedSiblingReexecuted"]
