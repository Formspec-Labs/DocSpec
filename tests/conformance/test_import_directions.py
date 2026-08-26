from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = ROOT / "src" / "docspec"

# Specification section 4.1 pins one dependency direction:
#
#   commands -> application services -> DocSpec ports and domain records <- adapters
#
# Each production area therefore declares the complete set of areas it may
# import. "errors" and "domain" sit at the bottom; "ports" speak only domain
# records; "processing" is DocSpec's own deterministic content implementation
# (vendor software enters it only through a lazily selected extractor, proven
# by test_pypdf_is_loaded_only_when_selected_and_pages_round_trip); application
# services compose ports, domain records, and processing receipts; adapters
# may depend on the whole core; only the cli composition root wires adapters
# into services. A new import direction fails here until someone widens the
# map deliberately.
_ALLOWED_INTERNAL_IMPORTS = {
    "__init__": {"errors"},
    "errors": set(),
    "domain": {"domain", "errors"},
    "ports": {"ports", "domain", "errors"},
    "processing": {"processing", "domain", "errors"},
    "profile_registry": {"domain", "errors"},
    "conformance": {"conformance", "domain", "errors", "__init__"},
    "application": {"application", "ports", "domain", "processing", "errors"},
    "adapters": {
        "adapters",
        "application",
        "ports",
        "domain",
        "processing",
        "errors",
        "__init__",
    },
    # Stable public assembly surface. It re-exports explicit constructors but
    # selects and instantiates none of them; the CLI remains the composition root.
    "source_catalog": {"adapters", "application", "domain", "ports"},
    "source_catalog_cli": {"adapters", "application", "domain", "errors"},
    "entrypoint": {"cli", "source_catalog_cli"},
    "cli": {
        "adapters",
        "application",
        "conformance",
        "domain",
        "errors",
        "ports",
        "processing",
        "profile_registry",
        "source_catalog_cli",
        "__init__",
    },
}

# Areas whose modules the core must never import back: concrete adapters and
# the operator command are the outermost ring.
_OUTER_AREAS = {"adapters", "cli", "entrypoint", "source_catalog", "source_catalog_cli"}
_CORE_AREAS = set(_ALLOWED_INTERNAL_IMPORTS) - _OUTER_AREAS
_PUBLIC_FACADE_MODULES = {"docspec.source_catalog"}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PRODUCTION_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _area(module: str) -> str:
    parts = module.split(".")
    return "__init__" if len(parts) == 1 else parts[1]


def _internal_imports(path: Path, module: str) -> set[str]:
    """Return every docspec module this file imports, absolute or relative."""

    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = module.split(".")
    if path.name != "__init__.py":
        package_parts = package_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.partition(".")[0] == "docspec")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.partition(".")[0] == "docspec":
                    imports.add(node.module)
                continue
            base = package_parts[: len(package_parts) - node.level + 1]
            resolved = ".".join(base + ([node.module] if node.module else []))
            if resolved.partition(".")[0] == "docspec":
                imports.add(resolved)
    return imports


def _production_modules() -> dict[str, Path]:
    modules = {_module_name(path): path for path in sorted(PRODUCTION_ROOT.rglob("*.py"))}
    assert modules, "the installed DocSpec package must contain production modules"
    return modules


def test_every_production_area_imports_only_its_declared_inner_layers() -> None:
    modules = _production_modules()
    observed_areas = {_area(module) for module in modules}
    assert observed_areas == set(_ALLOWED_INTERNAL_IMPORTS), (
        "a production area exists without a declared import direction"
    )

    violations: list[str] = []
    for module, path in modules.items():
        allowed = _ALLOWED_INTERNAL_IMPORTS[_area(module)]
        for imported in sorted(_internal_imports(path, module)):
            if _area(imported) not in allowed:
                violations.append(f"{module} imports {imported}")
    assert violations == []


def test_core_areas_never_import_adapters_or_the_command_surface() -> None:
    # The map above already forbids this; this check does not depend on the
    # map staying correct, so weakening the map cannot silently re-open the
    # outward direction the specification forbids.
    violations: list[str] = []
    for module, path in _production_modules().items():
        if _area(module) in _OUTER_AREAS:
            continue
        for imported in sorted(_internal_imports(path, module)):
            if _area(imported) in _OUTER_AREAS:
                violations.append(f"{module} imports {imported}")
    assert violations == []


def test_command_surfaces_are_explicit_composition_roots() -> None:
    modules = _production_modules()
    wiring = {
        module
        for module, path in modules.items()
        if _area(module) not in {"adapters"} and module not in _PUBLIC_FACADE_MODULES
        and any(_area(imported) == "adapters" for imported in _internal_imports(path, module))
    }
    assert wiring == {"docspec.cli", "docspec.source_catalog_cli"}
    assert {
        module
        for module in _PUBLIC_FACADE_MODULES
        if any(
            _area(imported) == "adapters"
            for imported in _internal_imports(modules[module], module)
        )
    } == _PUBLIC_FACADE_MODULES
    cli_imports = {_area(imported) for imported in _internal_imports(modules["docspec.cli"], "docspec.cli")}
    assert {"adapters", "application"} <= cli_imports
    importers_of_cli = {
        module
        for module, path in modules.items()
        if "docspec.cli" in _internal_imports(path, module)
    }
    assert importers_of_cli == {"docspec.entrypoint"}


def test_importing_the_complete_core_loads_no_vendor_software() -> None:
    """Prove at runtime what the static walk proves at rest: core imports stay
    stdlib-and-docspec even through lazy indirection, with every optional
    dependency installed and importable in this environment."""

    core_modules = sorted(
        module for module in _production_modules() if _area(module) in _CORE_AREAS
    )
    assert "docspec.application.execution" in core_modules
    assert "docspec.processing.extraction" in core_modules
    probe = (
        "import importlib, json, sys\n"
        "interpreter_baseline = {name.partition('.')[0] for name in sys.modules}\n"
        f"for name in {core_modules!r}:\n"
        "    importlib.import_module(name)\n"
        "loaded = {name.partition('.')[0] for name in sys.modules}\n"
        "foreign = loaded - interpreter_baseline - set(sys.stdlib_module_names) - {'docspec'}\n"
        "print(json.dumps(sorted(foreign)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert json.loads(result.stdout) == []
