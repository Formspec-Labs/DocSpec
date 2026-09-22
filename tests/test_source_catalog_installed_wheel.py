"""Installed-wheel proof for the source catalog: throwaway venvs install the built docspec wheel plus the pinned
vendor wheels, run the GAO and spicyregs-comments examples and the installed_source_catalog_probe, and verify
every published catalog with the installed CLI while proving the workspace path never leaks into proof bytes.

Also checks the built wheel vendors no wheel or spicy_docs package, reader-only imports avoid the optional
extras (boto3, httpx, polars, dagster, pypdf), and git tracks the vendored rulespec wheel.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

import docspec

ROOT = Path(__file__).resolve().parents[1]
RULESPEC_WHEEL = ROOT / "vendor" / "rulespec_artifacts-1.1.0-py3-none-any.whl"
RULESPEC_WHEEL_SHA256 = "3b2abcdcfa082f34baa3b03042c54fcc4e5e713901cd505cbd777dfd9bdf23cd"
PROVIDER = json.loads((ROOT / "vendor/spicy_docs.json").read_text(encoding="utf-8"))
SPICY_DOCS_WHEEL = ROOT / "vendor" / PROVIDER["filename"]
SPICY_DOCS_VERSION = PROVIDER["version"]
SPICY_DOCS_WHEEL_SHA256 = PROVIDER["sha256"]
SPICY_DOCS_REVISION = PROVIDER["sourceRevision"]


# This exact current producer wheel publishes and verifies the bounded source
# fixtures. Its version, source revision and digest are independent of the data
# pins produced below. The required core reader and this test use one wheel;
# this test never rebuilds it. Update these pins when accepting a new release.
_INSTALLED_PROBE = (ROOT / "tests/support/installed_source_catalog_probe.py").read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def test_installed_wheels_cover_source_kinds_reuse_and_independent_admission(
    tmp_path: Path,
    docspec_wheel: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the installed-wheel SourceCatalog proof requires uv"
    assert _sha256(RULESPEC_WHEEL) == RULESPEC_WHEEL_SHA256
    assert _sha256(SPICY_DOCS_WHEEL) == SPICY_DOCS_WHEEL_SHA256

    with zipfile.ZipFile(docspec_wheel) as archive:
        assert not any(
            name.endswith(".whl") or name.startswith("spicy_docs/")
            for name in archive.namelist()
        )

    runtime_root = tmp_path / "installed-runtime"
    wheelhouse = runtime_root / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    runtime_rulespec = shutil.copy2(RULESPEC_WHEEL, wheelhouse / RULESPEC_WHEEL.name)
    runtime_spicy_docs = shutil.copy2(SPICY_DOCS_WHEEL, wheelhouse / SPICY_DOCS_WHEEL.name)
    runtime_docspec = shutil.copy2(docspec_wheel, wheelhouse / docspec_wheel.name)
    environment = runtime_root / "environment"
    create = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert create.returncode == 0, create.stderr
    environment_python = environment / "bin" / "python"
    install_core = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(runtime_rulespec),
            str(runtime_spicy_docs),
            str(runtime_docspec),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_core.returncode == 0, install_core.stderr
    reader_only = subprocess.run(
        [environment_python, "-I", "-c",
         "import docspec.source_catalog, spicy_docs.source_native, importlib.util; "
         "assert all(importlib.util.find_spec(name) is None for name in "
         "('boto3', 'httpx', 'polars', 'dagster', 'pypdf'))"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert reader_only.returncode == 0, reader_only.stderr
    # Exercise the same GAO behavior test against installed packages. Source
    # publication uses injected fixture bytes and needs only the reader wheel.
    install_pytest = subprocess.run(
        [uv, "pip", "install", "--python", str(environment_python), f"pytest=={version('pytest')}"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert install_pytest.returncode == 0, install_pytest.stderr
    examples_root = runtime_root / "examples"
    examples_root.mkdir()
    for name in ("__init__.py", "gao_topics.py", "provider_identity.py", "dataset_example_support.py", "phrase_match_processor.py"):
        shutil.copy2(ROOT / "examples" / name, examples_root / name)
    shutil.copytree(ROOT / "examples/gao_fixtures", examples_root / "gao_fixtures")
    tests_root = runtime_root / "tests"
    tests_root.mkdir()
    shutil.copy2(ROOT / "tests/test_gao_topic_example.py", tests_root / "test_gao_topic_example.py")
    gao = subprocess.run(
        [environment_python, "-I", "-m", "pytest", "-q", "-o", f"pythonpath={runtime_root}",
         str(tests_root / "test_gao_topic_example.py")],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert gao.returncode == 0, gao.stdout + gao.stderr
    # The comment-table example needs the provider's existing Parquet extra.
    # Keep the core/reader-only absence checks above, and reuse this wheel/env.
    install_tables = subprocess.run(
        [uv, "pip", "install", "--python", str(environment_python),
         str(runtime_rulespec), f"{runtime_spicy_docs}[public-table]"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert install_tables.returncode == 0, install_tables.stderr
    shutil.copy2(ROOT / "examples/spicyregs_comments.py", examples_root / "spicyregs_comments.py")
    shutil.copy2(ROOT / "tests/test_spicyregs_comments_example.py", tests_root / "test_spicyregs_comments_example.py")
    comments = subprocess.run(
        [environment_python, "-I", "-m", "pytest", "-q", "-o", f"pythonpath={runtime_root}",
         str(tests_root / "test_spicyregs_comments_example.py")],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert comments.returncode == 0, comments.stdout + comments.stderr
    # The reader-only check above stays independent of acquisition. This next
    # fixture explicitly chooses the existing HTTP extra for its body fetch.
    install_http = subprocess.run(
        [uv, "pip", "install", "--python", str(environment_python), str(runtime_rulespec), f"{runtime_docspec}[http]"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert install_http.returncode == 0, install_http.stderr
    shutil.copy2(ROOT / "examples/offline/notice.html", runtime_root / "notice.html")
    dependency_check = subprocess.run(
        [uv, "pip", "check", "--python", str(environment_python)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert dependency_check.returncode == 0, dependency_check.stderr

    probe_path = runtime_root / "installed_source_catalog_probe.py"
    proof_path = runtime_root / "installed_source_catalog_proof.json"
    # Version comes from the tree, never a literal. Both of these were
    # hardcoded "0.2.10" and this test was excluded from the runs that cut
    # 0.2.11, so the bump would have been published with its own packaging
    # check silently stale -- a literal describing changing code, which is
    # the same defect the version bump existed to fix.
    probe_path.write_text(
        _INSTALLED_PROBE.replace("__DOCSPEC_VERSION__", docspec.__version__)
        .replace("__SPICY_DOCS_VERSION__", SPICY_DOCS_VERSION)
        .replace("__SPICY_DOCS_WHEEL_SHA256__", SPICY_DOCS_WHEEL_SHA256)
        .replace("__SPICY_DOCS_REVISION__", SPICY_DOCS_REVISION),
        encoding="utf-8",
    )
    probe = subprocess.run(
        [
            environment_python,
            "-I",
            str(probe_path),
            str(runtime_root),
            str(proof_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    proof = json.loads(proof_path.read_text(encoding="utf-8"))

    verify_environment = runtime_root / "verify-environment"
    create_verify = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(verify_environment)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert create_verify.returncode == 0, create_verify.stderr
    verify_python = verify_environment / "bin" / "python"
    install_verify = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(verify_python),
            str(runtime_rulespec),
            str(runtime_spicy_docs),
            str(runtime_docspec),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_verify.returncode == 0, install_verify.stderr
    isolated_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    isolated_environment["PYTHONNOUSERSITE"] = "1"
    optional_absence = subprocess.run(
        [
            verify_python,
            "-I",
            "-c",
            (
                "import importlib.util; "
                "assert all(importlib.util.find_spec(name) is None for name in ('pypdf', 'polars', 'dagster'))"
            ),
        ],
        cwd=tmp_path,
        env=isolated_environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert optional_absence.returncode == 0, optional_absence.stderr
    verify_references = runtime_root / "verify-references"
    verify_references.mkdir()
    docspec_implementation = "git+https://example.test/docspec@" + "1" * 40
    for index, build_report in enumerate(proof["buildReports"]):
        reference_path = verify_references / f"catalog-{index}.json"
        reference_path.write_text(
            json.dumps(build_report["catalog"], sort_keys=True),
            encoding="utf-8",
        )
        verification = subprocess.run(
            [
                verify_environment / "bin" / "docspec",
                "source-catalog",
                "verify",
                "--root",
                build_report["destination"],
                "--reference",
                str(reference_path),
                "--implementation-id",
                docspec_implementation,
                "--verifier-implementation-id",
                docspec_implementation,
            ],
            cwd=tmp_path,
            env=isolated_environment,
            capture_output=True,
            check=False,
            text=True,
        )
        assert verification.returncode == 0, verification.stderr

    workspace = ROOT.parent.resolve(strict=True).as_posix()
    assert workspace not in json.dumps(proof, sort_keys=True)
    assert proof["pythonVersion"].startswith("3.12.")
    assert proof["sourceProfiles"] == [
        "federal-register",
        "regulations-gov-documents",
        "regulations-gov-dockets",
        "regulations-gov-comments",
    ]
    assert len(proof["sourceNativePins"]) == 5
    assert set(proof["collectionOutcomes"]) == {"empty", "partial-rejection", "total-rejection"}
    assert len(proof["buildReports"]) == 4
    assert len(proof["admissions"]) == 4
    assert proof["existingDestinationRefusal"]["returnCode"] == 2
    initial, successor, physical_rebuild, regulations = proof["buildReports"]
    assert initial["catalog"]["catalogId"] == physical_rebuild["catalog"]["catalogId"]
    assert initial["catalog"]["digest"] != physical_rebuild["catalog"]["digest"]
    assert initial["catalog"]["catalogId"] != successor["catalog"]["catalogId"]
    assert regulations["itemCount"] == 3
    assert set(proof["regulationsItemScopes"]) == {
        "EPA-2026-0001",
        "EPA-2026-0001-0001",
        "EPA-2026-0001-9001",
    }
    assert len(proof["unchangedPartitions"]) == 2
    for path in runtime_root.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".txt"}:
            assert workspace.encode() not in path.read_bytes()


def test_the_vendored_wheel_is_tracked_by_git() -> None:
    """A bumped vendored wheel that git ignores is invisible until a fresh clone: vendor/.gitignore re-admits
    exactly one wheel by name, so this asks git's index what it would hand a new checkout instead of reading the
    working tree -- a failure this dependency has had three times.
    """

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(RULESPEC_WHEEL.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, (
        f"{RULESPEC_WHEEL.name} exists but git does not track it; "
        f"add it to vendor/.gitignore's allowlist. {tracked.stderr.strip()}"
    )
