from __future__ import annotations

import ast
import configparser
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from importlib.metadata import version
from pathlib import Path

from docspec import __version__
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.source_catalog import source_catalog_schemas

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = ROOT / "src" / "docspec"
SOURCE_CATALOG_SCHEMA_ROOT = PRODUCTION_ROOT / "schemas" / "source_catalog" / "1.0"
REPOSITORY_CODE_ROOTS = ("src", "tests", "tools")

# Match literal sibling checkout paths while allowing distribution names and
# remote repository URLs. Actual package imports are checked separately.
SIBLING_CHECKOUT_PATH = re.compile(r"(?:^|/)spicy[-_]regs(?:/|\Z)")
SIBLING_PACKAGE_ROOTS = frozenset({"spicy_regs", "spicyregs"})
# This one core module delegates canonical JSON to the shared public package.
SHARED_CANONICAL_GATEWAY = "src/docspec/domain/identity.py"
# An absolute path whose first segment is a home-directory root belongs to one
# developer's machine, so it can only reach code this repository does not own.
HOME_DIRECTORY_ROOTS = frozenset({"Users", "home"})

ADAPTER_ONLY_SIBLING_PACKAGES = frozenset(
    {
        "refspec",
        "rulespec",
        "spicy_docs",
        "spicy_regs",
        "spicyregs",
        "spicysearch",
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


def _repository_code_files() -> list[Path]:
    return sorted(path for name in REPOSITORY_CODE_ROOTS for path in (ROOT / name).rglob("*.py"))


def test_repository_code_avoids_sibling_imports_and_personal_checkout_paths() -> None:
    """Repository code consumes pinned inputs rather than sibling worktrees."""

    files = _repository_code_files()
    assert files, "the repository must contain code under src/, tests/, and tools/"

    violations: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                segments = value.split("/")
                if len(segments) > 1 and not segments[0] and segments[1] in HOME_DIRECTORY_ROOTS:
                    violations.append(f"{relative}:{node.lineno} names a home directory: {value!r}")
                if "/" in value and SIBLING_CHECKOUT_PATH.search(value):
                    violations.append(f"{relative}:{node.lineno} names a SpicyRegs path: {value!r}")

    for imported in (name for path in files for name in _absolute_imports(path)):
        if imported.partition(".")[0] in SIBLING_PACKAGE_ROOTS:
            violations.append(f"a repository module imports {imported}")

    assert violations == []


def test_project_declares_shared_artifact_utilities_and_one_command() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    provider = json.loads((ROOT / "vendor/spicy_docs.json").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__
    assert "rulespec-artifacts==1.0.12" in project["project"]["dependencies"]
    assert project["tool"]["uv"]["sources"]["rulespec-artifacts"] == {
        "path": "vendor/rulespec_artifacts-1.0.12-py3-none-any.whl"
    }
    assert project["tool"]["uv"]["sources"]["spicy-docs"] == {
        "path": "vendor/" + provider["filename"]
    }
    assert set(project["tool"]["uv"]["sources"]) == {"rulespec-artifacts", "spicy-docs"}
    assert project["project"]["scripts"] == {"docspec": "docspec.entrypoint:main"}
    assert set(project["project"]["optional-dependencies"]) == {
        "dagster",
        "http",
        "pdf",
        "s3",
        "spicy-docs",
        "tokens",
    }

    extras = project["project"]["optional-dependencies"]
    assert extras["spicy-docs"] == ["spicy-docs==" + provider["version"]]
    assert any(requirement.startswith("httpx") for requirement in extras["http"])
    assert extras["pdf"] == ["pypdf>=5,<7"]
    assert any(requirement.startswith("boto3") for requirement in extras["s3"])
    assert any(requirement.startswith("dagster") for requirement in extras["dagster"])
    assert any(requirement.startswith("tiktoken") for requirement in extras["tokens"])
    assert project["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_checked_in_source_catalog_schemas_equal_domain_generation() -> None:
    generated = source_catalog_schemas()

    assert {path.name for path in SOURCE_CATALOG_SCHEMA_ROOT.iterdir()} == set(generated)
    for name, schema in generated.items():
        assert (SOURCE_CATALOG_SCHEMA_ROOT / name).read_bytes() == canonical_json_file_bytes(schema)


def test_production_imports_stay_inside_the_standalone_boundary() -> None:
    files = _production_files()
    assert files, "the installed DocSpec package must contain production modules"

    violations: list[str] = []
    shared_imports = {(SHARED_CANONICAL_GATEWAY, "rulespec_artifacts")}
    observed_shared_imports: set[tuple[str, str]] = set()
    for path in files:
        relative_parts = path.relative_to(PRODUCTION_ROOT).parts
        is_adapter = relative_parts[0] == "adapters"
        is_composition_surface = relative_parts[0] in {"cli", "cli_io.py", "entrypoint.py", "runtime"}
        for imported in _absolute_imports(path):
            root_name = imported.partition(".")[0]
            edge = (path.relative_to(ROOT).as_posix(), imported)
            if edge in shared_imports:
                observed_shared_imports.add(edge)
            if not is_adapter and not is_composition_surface and root_name in ADAPTER_ONLY_SIBLING_PACKAGES:
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
            if (
                not is_adapter
                and not is_composition_surface
                and root_name != "docspec"
                and root_name not in sys.stdlib_module_names
                and edge not in shared_imports
            ):
                violations.append(f"{path.relative_to(ROOT)} imports undeclared core dependency {imported}")

    assert violations == []
    assert observed_shared_imports == shared_imports


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
        [sys.executable, "-I", "-m", "docspec.entrypoint", "--help"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "DocSpec" in help_result.stdout or "docspec" in help_result.stdout


def test_dagster_adapter_import_is_lazy() -> None:
    import docspec.adapters as adapters

    assert callable(adapters.build_dagster_definitions)
    isolated = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from docspec.adapters import build_dagster_definitions; assert 'dagster' not in sys.modules",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert isolated.returncode == 0, isolated.stderr


def test_installed_wheel_preserves_public_runtime_and_packaged_resources(tmp_path: Path, docspec_wheel: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the package release test requires uv"

    wheel = docspec_wheel

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert members
        assert all(name.startswith(("docspec/", "docspec-")) for name in members)
        assert not any(Path(name).suffix in {".pyc", ".pyo"} for name in members)

        profile_paths = sorted((PRODUCTION_ROOT / "storage_profiles").glob("*.json"))
        assert len(profile_paths) == 10
        for path in profile_paths:
            assert archive.read(f"docspec/storage_profiles/{path.name}") == path.read_bytes()

        for name, schema in source_catalog_schemas().items():
            member = f"docspec/schemas/source_catalog/1.0/{name}"
            assert member in members
            assert archive.read(member) == canonical_json_file_bytes(schema)

        entry_points_name = next(name for name in members if name.endswith(".dist-info/entry_points.txt"))
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points_name).decode("utf-8"))
        assert dict(parser["console_scripts"]) == {"docspec": "docspec.entrypoint:main"}

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
    # Until package publication is separately authorized, these exact wheels
    # are the metadata consumer installation bundle. No sibling checkout,
    # legacy document extra, or editable path participates in this test.
    install = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(ROOT / "vendor" / "rulespec_artifacts-1.0.12-py3-none-any.whl"),
            str(wheel),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    import_result = subprocess.run(
        [
            environment_python,
            "-I",
            "-c",
            (
                "import docspec; "
                "import docspec.entrypoint; "
                "from docspec.profile_registry import ProfileRegistry; "
                "registry = ProfileRegistry.builtin(); "
                "assert len(registry.list()) == 10; "
                "assert len(registry.local_profiles().pins) == 6; "
                "from pathlib import Path; "
                "from docspec.workspace import LocalWorkspace; "
                "assert LocalWorkspace(Path.cwd()).roots['blobStorage'] == Path.cwd() / 'blobStorage'; "
                "from docspec.adapters import build_dagster_definitions; "
                "assert callable(build_dagster_definitions); "
                "from docspec.source_catalog import requested_universe_set_digest; "
                "from docspec.source_catalog import AdmittedSourceCatalog, open_admitted_source_catalog; "
                "assert requested_universe_set_digest(0, ()).startswith('sha256:'); "
                "import importlib.util, sys; "
                    "import docspec.cli; "
                    "import docspec.adapters.platform_artifact; "
                "assert importlib.util.find_spec('rulespec_conformance') is None; "
                "assert importlib.util.find_spec('refspec') is None; "
                "assert importlib.util.find_spec('rdflib') is None; "
                "assert importlib.util.find_spec('dagster') is None"
            ),
        ],
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
    source_catalog_help = subprocess.run(
        [environment / "bin" / "docspec", "source-catalog", "--help"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert source_catalog_help.returncode == 0, source_catalog_help.stderr
    assert "source-catalog" in source_catalog_help.stdout

    profile_list = subprocess.run(
        [environment / "bin" / "docspec", "profile", "list"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert profile_list.returncode == 0, profile_list.stderr

    # Reuse the documented source fixture, but execute the typed API from the
    # installed wheel, outside the checkout and without a caller JSON request.
    examples = tmp_path / "examples"
    examples.mkdir()
    for filename in ("offline_demo.py", "phrase_match_processor.py"):
        shutil.copy2(ROOT / "examples" / filename, examples / filename)
    shutil.copytree(ROOT / "examples/offline", examples / "offline")
    shutil.copy2(ROOT / "tests/support/installed_runtime_probe.py", tmp_path / "runtime_probe.py")
    runtime_result = subprocess.run(
        [environment_python, "-I", str(tmp_path / "runtime_probe.py"), sha256_digest(wheel.read_bytes())],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert runtime_result.returncode == 0, runtime_result.stderr
    assert "retained capture, processed it without refetching, recovered" in runtime_result.stdout
    # Reuse the same behavioral test against the installed wheel: phase-by-phase
    # call observations belong in one test, not in the contributor example.
    install_test_runner = subprocess.run(
        [uv, "pip", "install", "--python", str(environment_python), f"pytest=={version('pytest')}"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert install_test_runner.returncode == 0, install_test_runner.stderr
    shutil.copy2(ROOT / "tests/test_offline_example.py", tmp_path / "test_offline_example.py")
    example_result = subprocess.run(
        [environment_python, "-I", "-c",
         "import pathlib, pytest, sys; "
         f"sys.path.insert(0, {str(tmp_path)!r}); "
         "import docspec; assert pathlib.Path(docspec.__file__).is_relative_to(sys.prefix); "
         "sys.exit(pytest.main(['-q', 'test_offline_example.py']))"],
        cwd=tmp_path, capture_output=True, check=False, text=True,
    )
    assert example_result.returncode == 0, example_result.stdout + example_result.stderr
    assert '"profileCount":10' in profile_list.stdout
