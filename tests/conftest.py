"""Optional complete-regression admission using pytest's own test reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_MAPPING = pytest.StashKey[dict[str, list[str]]]()
_COLLECTED = pytest.StashKey[set[str]]()
_REQUIRED = pytest.StashKey[dict[str, set[str] | None]]()
_PHASES = {"setup", "call", "teardown"}


@pytest.fixture(scope="session")
def docspec_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once per test session; consumers still install isolated environments."""
    import shutil
    import subprocess

    uv = shutil.which("uv")
    assert uv is not None, "installed package tests require uv"
    wheel_directory = tmp_path_factory.mktemp("docspec-wheel")
    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(wheel_directory)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheel_directory.glob("docspec-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-regression-map",
        action="store_true",
        help="Require every mapped regression test to be selected and pass all execution phases",
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate requirement ID: {key}")
        result[key] = value
    return result


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("require_regression_map"):
        return
    if any("::" in argument for argument in config.args):
        raise pytest.UsageError("required regression evidence needs whole test files or directories, not node selectors")
    path = config.rootpath / "conformance" / "test-matrix.json"
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("the regression map must be a nonempty object")
        for requirement, selectors in mapping.items():
            if not requirement or not isinstance(selectors, list) or not selectors:
                raise ValueError(f"{requirement!r} must have a nonempty selector list")
            if any(
                not isinstance(selector, str)
                or "::" not in selector
                or not selector.partition("::")[0].endswith(".py")
                or not selector.rpartition("::")[2]
                or "[" in selector
                for selector in selectors
            ):
                raise ValueError(f"{requirement!r} must name exact pytest function selectors")
            if len(set(selectors)) != len(selectors):
                raise ValueError(f"{requirement!r} repeats a selector")
    except (OSError, UnicodeError, ValueError) as error:
        raise pytest.UsageError(f"invalid required regression map: {error}") from error
    config.stash[_MAPPING] = mapping
    config.stash[_COLLECTED] = set()
    config.stash[_REQUIRED] = {}


def pytest_itemcollected(item: pytest.Item) -> None:
    if _COLLECTED in item.config.stash:
        item.config.stash[_COLLECTED].add(item.nodeid)


def pytest_collection_finish(session: pytest.Session) -> None:
    if _MAPPING not in session.config.stash:
        return
    collected = session.config.stash[_COLLECTED]
    selected = {item.nodeid for item in session.items}
    required = session.config.stash[_REQUIRED]
    for requirement, selectors in session.config.stash[_MAPPING].items():
        for selector in selectors:
            matches = {node for node in collected if node == selector or node.startswith(selector + "[")}
            if not matches:
                raise pytest.UsageError(f"required regression selector was not collected: {requirement}: {selector}")
            omitted = matches - selected
            if omitted:
                raise pytest.UsageError(f"required regression tests were deselected: {requirement}: {sorted(omitted)}")
            required.update((node, set()) for node in matches)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    required = item.config.stash.get(_REQUIRED, {})
    if item.nodeid in required and required[item.nodeid] is not None:
        if not report.passed or hasattr(report, "wasxfail"):
            required[item.nodeid] = None
        else:
            required[item.nodeid].add(report.when)
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _MAPPING not in session.config.stash:
        return
    required = session.config.stash.get(_REQUIRED, {})
    incomplete = sorted(node for node, phases in required.items() if phases != _PHASES)
    if not required:
        incomplete = ["required regression tests were not collected"]
    if not incomplete:
        return
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_sep("=", "required regression tests did not complete successfully")
        for node in incomplete:
            terminal.write_line(node)
