"""Exercise the --require-regression-map gate through native pytest collection and execution: every selector in
``conformance/test-matrix.json`` must be collected whole and complete every execution phase.

Builds isolated pytester projects and asserts refusals for missing or import-skipped selectors, deselected
parameter cases, positional case selection that hides sibling cases, skip/xfail/setup/teardown/early exits,
empty or duplicate map declarations; focused pytest runs without the flag stay unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


pytest_plugins = ("pytester",)
_SELECTOR = "tests/test_probe.py::test_probe"


def _project(pytester: pytest.Pytester, source: str, *, selectors: list[str] | None = None) -> None:
    """Write a throwaway project holding the repo conftest, a probe test module and a required-selector map."""
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    pytester.makeini("[pytest]\ntestpaths = tests\n")
    tests = pytester.path / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text(source, encoding="utf-8")
    conformance = pytester.path / "conformance"
    conformance.mkdir()
    (conformance / "test-matrix.json").write_text(
        json.dumps({"REQUIRED": [_SELECTOR] if selectors is None else selectors}), encoding="utf-8",
    )


def test_required_parameter_cases_pass_once_and_use_native_junit(pytester: pytest.Pytester) -> None:
    _project(pytester, "import pytest\n@pytest.mark.parametrize('value', [1, 2])\ndef test_probe(value):\n    assert value\n")
    result = pytester.runpytest_subprocess("--require-regression-map", "--junitxml=results.xml", "-q")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=2)
    cases = list(ET.parse(pytester.path / "results.xml").getroot().iter("testcase"))
    assert [case.attrib["name"] for case in cases] == ["test_probe[1]", "test_probe[2]"]


def test_ordinary_focused_pytest_does_not_require_the_map(pytester: pytest.Pytester) -> None:
    _project(pytester, "def test_probe():\n    pass\n", selectors=["tests/missing.py::test_missing"])
    result = pytester.runpytest_subprocess(_SELECTOR, "-q")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize(
    "source",
    [
        "def test_renamed():\n    pass\n",
        "import pytest\npytest.importorskip('docspec_required_dependency_does_not_exist')\ndef test_probe():\n    pass\n",
    ],
)
def test_missing_or_import_skipped_required_selector_refuses(pytester: pytest.Pytester, source: str) -> None:
    _project(pytester, source)
    result = pytester.runpytest_subprocess("--require-regression-map", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*required regression selector was not collected*REQUIRED*test_probe*"])


def test_deselecting_one_required_parameter_case_refuses(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        "import pytest\n@pytest.mark.parametrize('value', [1, 2], ids=['one', 'two'])\ndef test_probe(value):\n    pass\n",
    )
    result = pytester.runpytest_subprocess("--require-regression-map", "-k", "one", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "required regression tests were deselected" in result.stderr.str()
    assert "test_probe[two]" in result.stderr.str()


def test_positional_parameter_selection_cannot_hide_uncollected_siblings(pytester: pytest.Pytester) -> None:
    _project(pytester, "import pytest\n@pytest.mark.parametrize('value', [1, 2])\ndef test_probe(value):\n    pass\n")
    result = pytester.runpytest_subprocess("--require-regression-map", _SELECTOR + "[1]", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*required regression evidence needs whole test files or directories*"])


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\ndef test_probe():\n    pytest.skip('not executed')\n",
        "import pytest\ndef test_probe():\n    pytest.xfail('not established')\n",
        "import pytest\n@pytest.mark.xfail\ndef test_probe():\n    pass\n",
        "import pytest\n@pytest.fixture(autouse=True)\ndef setup():\n    pytest.skip('setup skipped')\ndef test_probe():\n    pass\n",
        "import pytest\n@pytest.fixture(autouse=True)\ndef setup():\n    assert False\ndef test_probe():\n    pass\n",
        "import pytest\n@pytest.fixture(autouse=True)\ndef setup():\n    yield\n    assert False\ndef test_probe():\n    pass\n",
    ],
    ids=["skip", "xfail", "xpass", "setup-skip", "setup-error", "teardown-error"],
)
def test_required_work_needs_all_successful_execution_phases(pytester: pytest.Pytester, source: str) -> None:
    _project(pytester, source)
    result = pytester.runpytest_subprocess("--require-regression-map", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required regression tests did not complete successfully*", _SELECTOR])


@pytest.mark.parametrize("option", ["--collect-only", "--setup-only"])
def test_collection_or_setup_without_execution_cannot_pass(pytester: pytest.Pytester, option: str) -> None:
    _project(pytester, "def test_probe():\n    pass\n")
    result = pytester.runpytest_subprocess("--require-regression-map", option, "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required regression tests did not complete successfully*"])


def test_early_success_exit_leaves_required_work_incomplete(pytester: pytest.Pytester) -> None:
    _project(pytester, "import pytest\ndef test_probe():\n    pytest.exit('early exit', returncode=0)\n")
    result = pytester.runpytest_subprocess("--require-regression-map", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required regression tests did not complete successfully*"])


def test_early_collection_exit_cannot_leave_the_required_population_unchecked(pytester: pytest.Pytester) -> None:
    _project(pytester, "def test_probe():\n    pass\n")
    with (pytester.path / "conftest.py").open("a", encoding="utf-8") as handle:
        handle.write("\ndef pytest_collection(session):\n    pytest.exit('before collection', returncode=0)\n")
    result = pytester.runpytest_subprocess("--require-regression-map", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required regression tests were not collected*"])


@pytest.mark.parametrize("contents", ['{}', '{"REQUIRED": []}', '{"REQUIRED": [], "REQUIRED": []}'])
def test_empty_or_duplicate_map_declarations_refuse(pytester: pytest.Pytester, contents: str) -> None:
    _project(pytester, "def test_probe():\n    pass\n")
    (pytester.path / "conformance" / "test-matrix.json").write_text(contents, encoding="utf-8")
    result = pytester.runpytest_subprocess("--require-regression-map", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*invalid required regression map*"])
