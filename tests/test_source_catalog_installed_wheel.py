from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import docspec

ROOT = Path(__file__).resolve().parents[1]
RULESPEC_WHEEL = ROOT / "vendor" / "rulespec_artifacts-1.0.12-py3-none-any.whl"
RULESPEC_WHEEL_SHA256 = "3f6c946c60ff2ddbe854fce7f74f4358ddb21e3ba3f6ad10caa8a0d8d59fd0a5"
SPICY_DOCS_WHEEL = ROOT / "vendor" / "spicy_docs-0.2.0-py3-none-any.whl"
SPICY_DOCS_VERSION = "0.2.0"
SPICY_DOCS_WHEEL_SHA256 = "ecaa5ebc15df7cad12952e5fdeb8e1cef71614471dfb81b5f43316049d246c4e"
SPICY_DOCS_REVISION = "296f20d05e0c32419ebae9d43cdfd19fe054238b"


# This exact current producer wheel publishes and verifies the bounded source
# fixtures. Its version, source revision and digest are independent of the data
# pins produced below. The optional provider extra and this test use one wheel;
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
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the installed-wheel SourceCatalog proof requires uv"
    assert _sha256(RULESPEC_WHEEL) == RULESPEC_WHEEL_SHA256
    assert _sha256(SPICY_DOCS_WHEEL) == SPICY_DOCS_WHEEL_SHA256

    build_root = tmp_path / "build"
    build_root.mkdir()
    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(build_root)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    docspec_wheel = next(build_root.glob(f"docspec-{docspec.__version__}-*.whl"))
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
            str(runtime_docspec),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_core.returncode == 0, install_core.stderr
    core_only = subprocess.run(
        [environment_python, "-I", "-c",
         "import docspec.runtime, importlib.util; assert importlib.util.find_spec('spicy_docs') is None"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert core_only.returncode == 0, core_only.stderr
    install_producer = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(runtime_rulespec),
            str(runtime_spicy_docs),
            f"{runtime_docspec}[spicy-docs]",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_producer.returncode == 0, install_producer.stderr
    reader_only = subprocess.run(
        [environment_python, "-I", "-c",
         "import docspec.source_catalog, spicy_docs.source_native, importlib.util; "
         "assert all(importlib.util.find_spec(name) is None for name in "
         "('boto3', 'httpx', 'polars', 'pyarrow', 'dagster', 'pypdf'))"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert reader_only.returncode == 0, reader_only.stderr
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
    producer_absence = subprocess.run(
        [
            verify_python,
            "-I",
            "-c",
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('spicy_docs') is None"
            ),
        ],
        cwd=tmp_path,
        env=isolated_environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert producer_absence.returncode == 0, producer_absence.stderr
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
    """A bumped wheel that git ignores is invisible until someone clones.

    ``vendor/.gitignore`` ignores everything and re-admits exactly one wheel by
    name, so bumping the vendored version silently requires moving that
    allowlist line too. Miss it and ``git add`` stages the deletion of the old
    wheel and nothing else: the working tree still has the file, every local
    check passes, and only a fresh checkout -- a worktree, a clone, CI -- gets
    an empty vendor directory that cannot install. That failure has happened
    three times on this dependency, so it is checked here rather than
    remembered.

    The pin tests above read the wheel from the working tree, which is exactly
    what cannot see this; this one asks git what it would hand a new checkout.
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
