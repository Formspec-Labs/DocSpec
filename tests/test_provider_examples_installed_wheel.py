"""Run provider examples outside both repositories using the same pinned wheels."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("example,provider_extra,example_files,fixture_directory,test_file,support_files", [
    pytest.param(
        "cfr", "acquisition",
        ("dataset_example_support.py", "govinfo_cfr.py", "govinfo_cfr_fetcher.py",
         "phrase_match_processor.py", "provider_identity.py"),
        "cfr_fixtures", "test_govinfo_cfr_example.py", ("support/__init__.py", "support/processing.py"),
        id="annual-cfr",
    ),
    pytest.param(
        "fec", None, ("fec_committees.py",), "fec_fixtures", "test_fec_committees_example.py", (),
        id="fec-metadata",
    ),
])
def test_provider_example_uses_installed_packages_and_replays_offline(
    tmp_path, docspec_wheel, example, provider_extra, example_files, fixture_directory, test_file, support_files,
):
    uv = shutil.which("uv")
    assert uv
    manifest = json.loads((ROOT / "vendor/spicy_docs.json").read_text())
    provider = ROOT / "vendor" / manifest["filename"]
    assert hashlib.sha256(provider.read_bytes()).hexdigest() == manifest["sha256"]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
    environment["PYTHONNOUSERSITE"] = "1"

    def run(arguments):
        result = subprocess.run([str(value) for value in arguments], cwd=tmp_path, env=environment,
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    docspec = Path(shutil.copy2(docspec_wheel, wheels))
    spicy_docs = Path(shutil.copy2(provider, wheels))
    rulespec = Path(shutil.copy2(ROOT / "vendor/rulespec_artifacts-1.0.12-py3-none-any.whl", wheels))
    venv = tmp_path / "environment"
    run([uv, "venv", "--python", sys.executable, venv])
    python = venv / "bin/python"
    provider_requirement = str(spicy_docs) + (f"[{provider_extra}]" if provider_extra else "")
    run([uv, "pip", "install", "--python", python, rulespec, docspec, provider_requirement, "pytest"])
    run([uv, "pip", "check", "--python", python])
    examples = tmp_path / "examples"
    examples.mkdir()
    for filename in ("__init__.py", *example_files):
        shutil.copy2(ROOT / "examples" / filename, examples / filename)
    shutil.copytree(ROOT / "examples" / fixture_directory, examples / fixture_directory)
    tests = tmp_path / "tests"
    tests.mkdir()
    for filename in ("__init__.py", test_file, *support_files):
        (tests / filename).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "tests" / filename, tests / filename)
    absent = ("dagster", "spicy_regs") + (("httpx", "boto3", "polars") if example == "fec" else ())
    run([python, "-c", "import docspec, spicy_docs, importlib.util; from pathlib import Path; "
         f"assert all(importlib.util.find_spec(name) is None for name in {absent!r}); "
         "assert all('site-packages' in Path(m.__file__).parts for m in (docspec, spicy_docs))"])
    run([python, "-m", "pytest", "-q", "tests/" + test_file])
