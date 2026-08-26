from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from docspec.conformance import ConformanceError, load_report, run_conformance, summarize_report
from docspec.domain.identity import canonical_json_file_bytes


def _definitions(
    root: Path,
    tests: list[dict[str, object]],
    *,
    commit: bool = True,
) -> tuple[Path, Path]:
    test_ids = [item["testId"] for item in tests]
    specification = {
        "format": "docspec-conformance-specification",
        "formatVersion": "1.0",
        "specificationId": "urn:docspec:test:specification",
        "title": "Test specification",
        "normativeSource": "specification.md",
        "implementationStatus": "test",
        "conformanceVerdict": "not-yet-conformant",
        "verdictReason": "Executable test fixture",
        "requiredTestIds": test_ids,
        "evidencePolicy": {
            "reportFormat": "docspec-conformance-report/1.0",
            "missingRequiredTestVerdict": "fail",
            "skippedRequiredTestVerdict": "fail",
            "xfailedRequiredTestVerdict": "fail",
            "proseEstablishesConformance": False,
        },
    }
    matrix = {
        "format": "docspec-conformance-test-matrix",
        "formatVersion": "1.0",
        "specificationId": specification["specificationId"],
        "implementationStatus": "test",
        "tests": tests,
    }
    conformance = root / "conformance"
    conformance.mkdir(parents=True)
    specification_path = conformance / "specification.json"
    matrix_path = conformance / "test-matrix.json"
    specification_path.write_text(json.dumps(specification, indent=2) + "\n", encoding="utf-8")
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    if commit:
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "docspec-tests@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "DocSpec Tests"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=root, check=True)
    return specification_path, matrix_path


def _test_definition(
    test_id: str,
    selector: str | None,
    *,
    status: str = "implemented",
) -> dict[str, object]:
    return {
        "testId": test_id,
        "status": status,
        "selectors": [] if selector is None else [selector],
        "plannedTestModule": None if status == "implemented" else "tests/test_probe.py",
    }


def test_conformance_runner_executes_exact_selectors_and_seals_a_report(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text("def test_pass():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("CORE", "tests/test_probe.py::test_pass")],
    )
    output = tmp_path / "evidence" / "report.json"
    report = run_conformance(
        source_root=tmp_path,
        specification_path=specification,
        matrix_path=matrix,
        output_path=output,
        timeout_seconds=30,
    )
    assert report["verdict"] == "pass"
    assert report["firstFailureCode"] is None
    assert report["requiredTests"][0]["collected"] == 1
    assert report["requiredTests"][0]["passed"] == 1
    assert output.read_bytes() == canonical_json_file_bytes(report)
    assert load_report(output) == report
    assert summarize_report(output)["passedTestCount"] == 1

    with pytest.raises(ConformanceError, match="refusing to replace"):
        run_conformance(
            source_root=tmp_path,
            specification_path=specification,
            matrix_path=matrix,
            output_path=output,
            timeout_seconds=30,
        )


@pytest.mark.parametrize(
    ("status", "probe", "expected_code"),
    [
        ("partial", "def test_probe():\n    assert True\n", "DOCSPEC-CONFORMANCE-TEST-NOT-IMPLEMENTED"),
        ("implemented", "import pytest\ndef test_probe():\n    pytest.skip('not evidence')\n", "DOCSPEC-CONFORMANCE-UNSUPPORTED-TEST-OUTCOME"),
        ("implemented", "import pytest\n@pytest.mark.xfail\ndef test_probe():\n    assert False\n", "DOCSPEC-CONFORMANCE-UNSUPPORTED-TEST-OUTCOME"),
    ],
)
def test_partial_skipped_and_xfailed_proof_fail_closed(
    tmp_path: Path,
    status: str,
    probe: str,
    expected_code: str,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text(probe, encoding="utf-8")
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("REQUIRED", "tests/test_probe.py::test_probe", status=status)],
    )
    output = tmp_path / "report.json"
    report = run_conformance(
        source_root=tmp_path,
        specification_path=specification,
        matrix_path=matrix,
        output_path=output,
        timeout_seconds=30,
    )
    assert report["verdict"] == "fail"
    assert report["firstFailureCode"] == expected_code
    assert report["requiredTests"][0]["failureCode"] == expected_code


def test_missing_selector_is_recorded_as_absent_without_fake_success(tmp_path: Path) -> None:
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("MISSING", None, status="planned")],
    )
    output = tmp_path / "report.json"
    report = run_conformance(
        source_root=tmp_path,
        specification_path=specification,
        matrix_path=matrix,
        output_path=output,
        timeout_seconds=30,
    )
    evidence = report["requiredTests"][0]
    assert evidence["collected"] == 0
    assert evidence["verdict"] == "fail"
    assert evidence["failureCode"] == "DOCSPEC-CONFORMANCE-TEST-ABSENT"


def test_unsafe_selector_is_rejected_before_execution(tmp_path: Path) -> None:
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("UNSAFE", "../outside.py::test_probe")],
    )
    output = tmp_path / "report.json"
    with pytest.raises(ConformanceError, match="contained relative"):
        run_conformance(
            source_root=tmp_path,
            specification_path=specification,
            matrix_path=matrix,
            output_path=output,
            timeout_seconds=30,
        )
    assert not output.exists()


def test_report_reader_rejects_unknown_fields_even_when_json_is_valid(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("CORE", "tests/test_probe.py::test_pass")],
    )
    output = tmp_path / "report.json"
    report = run_conformance(
        source_root=tmp_path,
        specification_path=specification,
        matrix_path=matrix,
        output_path=output,
        timeout_seconds=30,
    )
    tampered = {**report, "unknown": True}
    output.unlink()
    output.write_bytes(canonical_json_file_bytes(tampered))
    with pytest.raises(ConformanceError, match="invalid closed shape"):
        load_report(output)


def test_conformance_runner_refuses_a_non_git_source(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("CORE", "tests/test_probe.py::test_pass")],
        commit=False,
    )

    with pytest.raises(ConformanceError, match="Git checkout"):
        run_conformance(
            source_root=tmp_path,
            specification_path=specification,
            matrix_path=matrix,
            output_path=tmp_path / "report.json",
            timeout_seconds=30,
        )


def test_conformance_runner_refuses_a_dirty_git_source(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    probe = tests / "test_probe.py"
    probe.write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    specification, matrix = _definitions(
        tmp_path,
        [_test_definition("CORE", "tests/test_probe.py::test_pass")],
    )
    probe.write_text("def test_pass():\n    assert False\n", encoding="utf-8")

    with pytest.raises(ConformanceError, match="must be clean"):
        run_conformance(
            source_root=tmp_path,
            specification_path=specification,
            matrix_path=matrix,
            output_path=tmp_path / "report.json",
            timeout_seconds=30,
        )
