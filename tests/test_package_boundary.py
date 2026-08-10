from __future__ import annotations

import ast
import configparser
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from docspec import __version__
from tools.generate_archive_manifest import manifest_bytes


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = ROOT / "src" / "docspec"
ARCHIVE_ROOT = ROOT / "archive" / "legacy-2026-08-05"

ADAPTER_ONLY_SIBLING_PACKAGES = frozenset(
    {
        "refspec",
        "rulespec",
        "spicy_regs",
        "spicyregs",
        "spicysearch",
    }
)
ARCHIVED_PRODUCT_AREAS = frozenset(
    {
        "candidate_release",
        "corpora",
        "docpipeline",
        "document_file_pipeline",
        "document_release",
        "document_release_v3",
        "document_release_v3_cli",
        "document_release_v3_compact",
        "document_release_v3_diff",
        "document_release_v3_verify",
        "document_release_v3_writer",
        "enrichment",
        "evaluate_tag_quality",
        "evaluation_boundary",
        "ontology",
        "pipelines",
        "published",
        "retrieval",
        "rulespec_testbed",
        "source_profile_artifacts",
        "source_profile_artifacts_cli",
        "source_profiles",
        "sources",
        "transforms",
    }
)


def _production_files() -> list[Path]:
    return sorted(PRODUCTION_ROOT.rglob("*.py"))


def _absolute_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def test_project_declares_a_stdlib_core_and_one_command() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == []
    assert project["project"]["scripts"] == {"docspec": "docspec.cli:main"}
    assert set(project["project"]["optional-dependencies"]) == {"dagster", "pdf", "s3"}

    extras = project["project"]["optional-dependencies"]
    assert any(requirement.startswith("pymupdf") for requirement in extras["pdf"])
    assert any(requirement.startswith("pypdf") for requirement in extras["pdf"])
    assert any(requirement.startswith("boto3") for requirement in extras["s3"])
    assert any(requirement.startswith("dagster") for requirement in extras["dagster"])
    assert "archive" in project["tool"]["ruff"]["exclude"]
    assert project["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_production_imports_stay_inside_the_standalone_boundary() -> None:
    files = _production_files()
    assert files, "the installed DocSpec package must contain production modules"

    violations: list[str] = []
    for path in files:
        relative_parts = path.relative_to(PRODUCTION_ROOT).parts
        is_adapter = relative_parts[0] == "adapters"
        for imported in _absolute_imports(path):
            root_name = imported.partition(".")[0]
            if not is_adapter and root_name in ADAPTER_ONLY_SIBLING_PACKAGES:
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
            if not is_adapter and root_name != "docspec" and root_name not in sys.stdlib_module_names:
                violations.append(f"{path.relative_to(ROOT)} imports non-stdlib core dependency {imported}")
            if imported.startswith("docspec."):
                area = imported.split(".", 2)[1]
                if area in ARCHIVED_PRODUCT_AREAS:
                    violations.append(f"{path.relative_to(ROOT)} imports archived area {imported}")

    assert violations == []


def test_archived_product_areas_are_absent_from_production() -> None:
    assert ARCHIVE_ROOT.is_dir()
    archive_manifest_path = ARCHIVE_ROOT / "archive.json"
    assert archive_manifest_path.is_file()

    top_level_names = {path.stem if path.is_file() else path.name for path in PRODUCTION_ROOT.iterdir()}
    assert top_level_names.isdisjoint(ARCHIVED_PRODUCT_AREAS)

    violations: list[str] = []
    for path in _production_files():
        if path.relative_to(PRODUCTION_ROOT).parts[0] == "adapters":
            continue
        source = path.read_text(encoding="utf-8").casefold()
        for word in ADAPTER_ONLY_SIBLING_PACKAGES:
            if re.search(rf"\b{re.escape(word.casefold())}\b", source):
                violations.append(f"{path.relative_to(ROOT)} names {word}")
    assert violations == []


def test_archive_manifest_is_generated_from_the_archive_and_up_to_date() -> None:
    # archive.json used to enumerate all 587 archived files individually (byteSize,
    # sha256, category, reason, originalPath -- ~268 KB) with no generator: touching
    # anything under the frozen archive/ tree meant hand-editing 588 entries by hand,
    # for content nothing outside this one test ever read.
    #
    # tools/generate_archive_manifest.py replaces the enumeration with a single
    # content-addressed tree digest over every archived file's path and bytes, so
    # this test only has to confirm the checked-in manifest is exactly what the
    # generator currently produces from the files actually on disk -- any archived
    # file added, removed, or changed fails here instead of the manifest quietly
    # going stale.
    manifest_path = ARCHIVE_ROOT / "archive.json"
    assert manifest_path.read_bytes() == manifest_bytes()


def test_core_import_and_cli_help_need_no_optional_dependency() -> None:
    import_result = subprocess.run(
        [sys.executable, "-I", "-c", "import docspec"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert import_result.returncode == 0, import_result.stderr

    help_result = subprocess.run(
        [sys.executable, "-I", "-m", "docspec.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "DocSpec" in help_result.stdout or "docspec" in help_result.stdout


def test_built_wheel_contains_only_the_standalone_package(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the package release test requires uv"

    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("docspec-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert members
        assert all(name.startswith(("docspec/", "docspec-")) for name in members)
        assert not any("archive" in Path(name).parts for name in members)
        assert not any(Path(name).suffix in {".pyc", ".pyo"} for name in members)

        packaged_areas = {
            parts[1]
            for name in members
            if name.startswith("docspec/")
            for parts in [Path(name).parts]
            if len(parts) > 2
        }
        assert packaged_areas.isdisjoint(ARCHIVED_PRODUCT_AREAS)

        entry_points_name = next(name for name in members if name.endswith(".dist-info/entry_points.txt"))
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points_name).decode("utf-8"))
        assert dict(parser["console_scripts"]) == {"docspec": "docspec.cli:main"}

    environment = tmp_path / "wheel-environment"
    create_environment = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert create_environment.returncode == 0, create_environment.stderr
    environment_python = environment / "bin" / "python"
    install = subprocess.run(
        [uv, "pip", "install", "--python", str(environment_python), str(wheel)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    import_result = subprocess.run(
        [environment_python, "-I", "-c", "import docspec"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert import_result.returncode == 0, import_result.stderr
    help_result = subprocess.run(
        [environment / "bin" / "docspec", "--help"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "DocSpec" in help_result.stdout or "docspec" in help_result.stdout
