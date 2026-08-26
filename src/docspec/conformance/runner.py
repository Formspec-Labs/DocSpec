"""Run required pytest selectors and seal a fail-closed conformance report."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docspec import __version__
from docspec.domain.identity import (
    canonical_json_file_bytes,
    identity_digest,
    parse_closed_json,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.errors import DocSpecError

_SPEC_KEYS = {
    "format",
    "formatVersion",
    "specificationId",
    "title",
    "normativeSource",
    "implementationStatus",
    "conformanceVerdict",
    "verdictReason",
    "requiredTestIds",
    "evidencePolicy",
}
_POLICY_KEYS = {
    "reportFormat",
    "missingRequiredTestVerdict",
    "skippedRequiredTestVerdict",
    "xfailedRequiredTestVerdict",
    "proseEstablishesConformance",
}
_MATRIX_KEYS = {"format", "formatVersion", "specificationId", "implementationStatus", "tests"}
_MATRIX_TEST_KEYS = {"testId", "status", "selectors", "plannedTestModule"}
_REPORT_KEYS = {
    "format",
    "formatVersion",
    "reportId",
    "specificationId",
    "specificationVersion",
    "conformanceClass",
    "source",
    "artifacts",
    "planAndConfiguration",
    "command",
    "environment",
    "requiredTests",
    "documentStores",
    "bytes",
    "measurements",
    "verifier",
    "firstFailureCode",
    "verdict",
    "generatedAt",
}
_TEST_RESULT_KEYS = {
    "testId",
    "declaredStatus",
    "selectors",
    "collected",
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "exitCode",
    "durationMilliseconds",
    "outputDigest",
    "outputByteSize",
    "failureCode",
    "verdict",
}
_ARTIFACT_EVIDENCE_KEYS = {"identity", "role", "locator", "digest", "byteSize"}
_MAX_MACHINE_FILE_BYTES = 16 * 1024 * 1024
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ConformanceError(DocSpecError):
    """A conformance definition or evidence report is incomplete or unsafe."""


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ConformanceError(f"{label} must be a regular, non-symlink file: {path}")
    if path.stat().st_size > _MAX_MACHINE_FILE_BYTES:
        raise ConformanceError(f"{label} exceeds the {_MAX_MACHINE_FILE_BYTES}-byte limit")
    try:
        value = thaw_json(parse_closed_json(path.read_bytes(), label=label))
    except DocSpecError as error:
        raise ConformanceError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise ConformanceError(f"{label} must be a JSON object")
    return value


def _require_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ConformanceError(f"{label} has an invalid closed shape; missing={missing}, extra={extra}")


def _load_definitions(specification_path: Path, matrix_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    specification = _read_json_object(specification_path, label="conformance specification")
    _require_keys(specification, _SPEC_KEYS, label="conformance specification")
    if (
        specification["format"] != "docspec-conformance-specification"
        or specification["formatVersion"] != "1.0"
    ):
        raise ConformanceError("conformance specification has an unknown format")
    policy = specification["evidencePolicy"]
    if not isinstance(policy, dict):
        raise ConformanceError("conformance evidence policy must be an object")
    _require_keys(policy, _POLICY_KEYS, label="conformance evidence policy")
    if policy != {
        "reportFormat": "docspec-conformance-report/1.0",
        "missingRequiredTestVerdict": "fail",
        "skippedRequiredTestVerdict": "fail",
        "xfailedRequiredTestVerdict": "fail",
        "proseEstablishesConformance": False,
    }:
        raise ConformanceError("conformance evidence policy does not fail closed")
    required = specification["requiredTestIds"]
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(item, str) or not item for item in required)
        or len(set(required)) != len(required)
    ):
        raise ConformanceError("required test identifiers must be a non-empty, distinct string list")

    matrix = _read_json_object(matrix_path, label="conformance test matrix")
    _require_keys(matrix, _MATRIX_KEYS, label="conformance test matrix")
    if matrix["format"] != "docspec-conformance-test-matrix" or matrix["formatVersion"] != "1.0":
        raise ConformanceError("conformance test matrix has an unknown format")
    if matrix["specificationId"] != specification["specificationId"]:
        raise ConformanceError("conformance test matrix names a different specification")
    tests = matrix["tests"]
    if not isinstance(tests, list):
        raise ConformanceError("conformance test matrix tests must be a list")
    test_ids: list[str] = []
    for index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise ConformanceError(f"conformance test matrix entry {index} must be an object")
        _require_keys(test, _MATRIX_TEST_KEYS, label=f"conformance test matrix entry {index}")
        test_id = test["testId"]
        selectors = test["selectors"]
        if not isinstance(test_id, str) or not test_id:
            raise ConformanceError(f"conformance test matrix entry {index} has no test identifier")
        if test["status"] not in {"planned", "partial", "implemented"}:
            raise ConformanceError(f"conformance test {test_id} has an unknown implementation status")
        if not isinstance(selectors, list) or any(not isinstance(item, str) or not item for item in selectors):
            raise ConformanceError(f"conformance test {test_id} selectors must be a string list")
        if len(set(selectors)) != len(selectors):
            raise ConformanceError(f"conformance test {test_id} repeats a selector")
        planned = test["plannedTestModule"]
        if planned is not None and (not isinstance(planned, str) or not planned):
            raise ConformanceError(f"conformance test {test_id} has an invalid planned module")
        test_ids.append(test_id)
    if len(set(test_ids)) != len(test_ids):
        raise ConformanceError("conformance test matrix repeats a test identifier")
    if test_ids != required:
        raise ConformanceError("conformance test matrix must list every required test once in specification order")
    return specification, matrix


def _safe_selector(root: Path, selector: str) -> str:
    if "\x00" in selector or selector.startswith("-"):
        raise ConformanceError(f"unsafe pytest selector: {selector!r}")
    path_text = selector.split("::", 1)[0]
    path = Path(path_text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ConformanceError(f"pytest selector must name a contained relative test path: {selector!r}")
    target = (root / path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ConformanceError(f"pytest selector escapes the source root: {selector!r}") from error
    return selector


def _junit_counts(path: Path) -> dict[str, int]:
    counts = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0}
    if not path.is_file():
        return counts
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ConformanceError(f"pytest did not produce readable JUnit evidence: {error}") from error
    for case in root.iter("testcase"):
        counts["collected"] += 1
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            counts["failed"] += 1
        elif error is not None:
            counts["errors"] += 1
        elif skipped is not None:
            counts["skipped"] += 1
            skip_type = (skipped.get("type") or "").lower()
            skip_message = (skipped.get("message") or "").lower()
            if "xfail" in skip_type or "xfail" in skip_message:
                counts["xfailed"] += 1
        else:
            counts["passed"] += 1
    return counts


def _run_required_test(
    root: Path,
    definition: dict[str, Any],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    test_id = definition["testId"]
    status = definition["status"]
    selectors = tuple(_safe_selector(root, item) for item in definition["selectors"])
    empty_counts = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0}
    if not selectors:
        return {
            "testId": test_id,
            "declaredStatus": status,
            "selectors": [],
            **empty_counts,
            "exitCode": None,
            "durationMilliseconds": 0,
            "outputDigest": sha256_digest(b""),
            "outputByteSize": 0,
            "failureCode": "DOCSPEC-CONFORMANCE-TEST-ABSENT",
            "verdict": "fail",
        }

    with tempfile.TemporaryDirectory(prefix="docspec-conformance-") as directory:
        junit = Path(directory) / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "--tb=short",
            "-rA",
            f"--junitxml={junit}",
            *selectors,
        ]
        environment = os.environ.copy()
        environment.pop("PYTEST_ADDOPTS", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic_ns()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
            )
            exit_code: int | None = completed.returncode
            output = completed.stdout + b"\x00" + completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            exit_code = None
            stdout = error.stdout if isinstance(error.stdout, bytes) else (error.stdout or "").encode("utf-8")
            stderr = error.stderr if isinstance(error.stderr, bytes) else (error.stderr or "").encode("utf-8")
            output = stdout + b"\x00" + stderr
        duration = max(0, (time.monotonic_ns() - started) // 1_000_000)
        counts = _junit_counts(junit)

    failure_code: str | None = None
    if status != "implemented":
        failure_code = "DOCSPEC-CONFORMANCE-TEST-NOT-IMPLEMENTED"
    elif timed_out:
        failure_code = "DOCSPEC-CONFORMANCE-TEST-TIMEOUT"
    elif counts["collected"] == 0:
        failure_code = "DOCSPEC-CONFORMANCE-TEST-ABSENT"
    elif counts["skipped"] or counts["xfailed"]:
        failure_code = "DOCSPEC-CONFORMANCE-UNSUPPORTED-TEST-OUTCOME"
    elif exit_code != 0 or counts["failed"] or counts["errors"]:
        failure_code = "DOCSPEC-CONFORMANCE-TEST-FAILED"
    verdict = "pass" if failure_code is None else "fail"
    return {
        "testId": test_id,
        "declaredStatus": status,
        "selectors": list(selectors),
        **counts,
        "exitCode": exit_code,
        "durationMilliseconds": duration,
        "outputDigest": sha256_digest(output),
        "outputByteSize": len(output),
        "failureCode": failure_code,
        "verdict": verdict,
    }


def _git_revision(root: Path) -> str:
    """Return the exact clean Git commit that contains the tested source."""

    try:
        top_level = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
        )
        revision_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConformanceError("conformance source must be a clean Git checkout") from error

    try:
        repository_root = Path(top_level.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConformanceError("conformance source must be a clean Git checkout") from error
    revision = revision_result.stdout.strip()
    if (
        top_level.returncode != 0
        or revision_result.returncode != 0
        or status.returncode != 0
        or repository_root != root
        or _GIT_COMMIT_PATTERN.fullmatch(revision) is None
    ):
        raise ConformanceError("conformance source must be the root of a Git checkout")
    if status.stdout:
        raise ConformanceError("conformance source Git checkout must be clean")
    return revision


def _write_new(path: Path, payload: bytes) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise ConformanceError(f"refusing to replace existing conformance report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ConformanceError(f"refusing to replace existing conformance report: {path}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def run_conformance(
    *,
    source_root: Path,
    specification_path: Path,
    matrix_path: Path,
    output_path: Path,
    conformance_class: str = "core",
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Execute every required selector and write one immutable evidence report."""

    root = Path(source_root)
    if root.is_symlink() or not root.is_dir():
        raise ConformanceError(f"source root must be a regular directory: {root}")
    root = root.resolve(strict=True)
    if not conformance_class or any(character.isspace() for character in conformance_class):
        raise ConformanceError("conformance class must be one non-empty token")
    if timeout_seconds <= 0:
        raise ConformanceError("test timeout must be positive")
    specification_path = Path(specification_path).resolve(strict=True)
    matrix_path = Path(matrix_path).resolve(strict=True)
    specification, matrix = _load_definitions(specification_path, matrix_path)
    output_path = Path(output_path)
    if output_path.exists() or output_path.is_symlink():
        raise ConformanceError(f"refusing to replace existing conformance report: {output_path}")

    revision = _git_revision(root)
    try:
        specification_locator = specification_path.relative_to(root).as_posix()
        matrix_locator = matrix_path.relative_to(root).as_posix()
    except ValueError as error:
        raise ConformanceError(
            "conformance specification and matrix must belong to the tested Git checkout"
        ) from error
    started = time.monotonic_ns()
    test_results = [
        _run_required_test(root, definition, timeout_seconds=timeout_seconds) for definition in matrix["tests"]
    ]
    wall_time = max(0, (time.monotonic_ns() - started) // 1_000_000)
    first_failure = next((item["failureCode"] for item in test_results if item["verdict"] == "fail"), None)
    verdict = "pass" if first_failure is None else "fail"
    configuration = {
        "conformanceClass": conformance_class,
        "sourceRevision": revision,
        "specificationPath": specification_locator,
        "testMatrixPath": matrix_locator,
        "timeoutSeconds": timeout_seconds,
    }
    command_arguments = [
        "docspec",
        "conformance",
        "run",
        "--root",
        root.as_posix(),
        "--specification",
        specification_path.as_posix(),
        "--matrix",
        matrix_path.as_posix(),
        "--output",
        output_path.resolve(strict=False).as_posix(),
        "--class",
        conformance_class,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    environment_content = {
        "pythonImplementation": platform.python_implementation(),
        "pythonVersion": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "docspecVersion": __version__,
    }
    body: dict[str, Any] = {
        "format": "docspec-conformance-report",
        "formatVersion": "1.0",
        "specificationId": specification["specificationId"],
        "specificationVersion": specification["formatVersion"],
        "conformanceClass": conformance_class,
        "source": {"revision": revision},
        "artifacts": {"inputs": [], "outputs": []},
        "planAndConfiguration": {
            "planId": None,
            "configurationId": stable_urn("conformance-configuration", configuration),
            "configurationDigest": identity_digest(configuration),
        },
        "command": {
            "identity": stable_urn("command", command_arguments),
            "arguments": command_arguments,
            "workingDirectory": root.as_posix(),
        },
        "environment": {
            "identity": stable_urn("environment", environment_content),
            **environment_content,
        },
        "requiredTests": test_results,
        "documentStores": {"storeCount": 0, "counts": {}, "dispositions": {}, "retries": 0, "failures": []},
        "bytes": {"read": 0, "reused": 0, "written": 0, "delivered": 0, "published": 0},
        "measurements": {"wallTimeMilliseconds": wall_time, "peakMemoryBytes": None},
        "verifier": {"implementationId": "docspec.conformance.pytest-subprocess.v1", "version": __version__},
        "firstFailureCode": first_failure,
        "verdict": verdict,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    report = {**body, "reportId": stable_urn("conformance-report", body)}
    _write_new(output_path, canonical_json_file_bytes(report))
    return report


def load_report(path: Path) -> dict[str, Any]:
    """Load one canonical report, validate its closed shape, and recheck identity."""

    path = Path(path)
    report = _read_json_object(path, label="conformance report")
    _require_keys(report, _REPORT_KEYS, label="conformance report")
    if report["format"] != "docspec-conformance-report" or report["formatVersion"] != "1.0":
        raise ConformanceError("conformance report has an unknown format")
    if path.read_bytes() != canonical_json_file_bytes(report):
        raise ConformanceError("conformance report is not canonical UTF-8 JSON")
    _validate_report_shape(report)
    body = {key: value for key, value in report.items() if key != "reportId"}
    if report["reportId"] != stable_urn("conformance-report", body):
        raise ConformanceError("conformance report identity differs from its content")
    tests = report["requiredTests"]
    if not isinstance(tests, list) or not tests:
        raise ConformanceError("conformance report contains no required test evidence")
    actual_first = next(
        (item.get("failureCode") for item in tests if isinstance(item, dict) and item.get("verdict") == "fail"),
        None,
    )
    if report["firstFailureCode"] != actual_first:
        raise ConformanceError("conformance report first failure code differs from required tests")
    actual_verdict = "pass" if actual_first is None else "fail"
    if report["verdict"] != actual_verdict:
        raise ConformanceError("conformance report verdict differs from required tests")
    return report


def _validate_report_shape(report: dict[str, Any]) -> None:
    nested_shapes = {
        "source": {"revision"},
        "artifacts": {"inputs", "outputs"},
        "planAndConfiguration": {"planId", "configurationId", "configurationDigest"},
        "command": {"identity", "arguments", "workingDirectory"},
        "environment": {
            "identity",
            "pythonImplementation",
            "pythonVersion",
            "platform",
            "machine",
            "docspecVersion",
        },
        "documentStores": {"storeCount", "counts", "dispositions", "retries", "failures"},
        "bytes": {"read", "reused", "written", "delivered", "published"},
        "measurements": {"wallTimeMilliseconds", "peakMemoryBytes"},
        "verifier": {"implementationId", "version"},
    }
    for name, keys in nested_shapes.items():
        value = report[name]
        if not isinstance(value, dict):
            raise ConformanceError(f"conformance report {name} must be an object")
        _require_keys(value, keys, label=f"conformance report {name}")
    for name in (
        "reportId",
        "specificationId",
        "specificationVersion",
        "conformanceClass",
        "generatedAt",
    ):
        try:
            require_text(report[name], f"conformance report {name}")
        except ValueError as error:
            raise ConformanceError(str(error)) from error
    source = report["source"]
    if not isinstance(source["revision"], str) or _GIT_COMMIT_PATTERN.fullmatch(source["revision"]) is None:
        raise ConformanceError("conformance report source revision must be a full lowercase Git commit")
    artifacts = report["artifacts"]
    for direction in ("inputs", "outputs"):
        values = artifacts[direction]
        if not isinstance(values, list):
            raise ConformanceError(f"conformance report artifact {direction} must be a list")
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ConformanceError(f"conformance report artifact {direction} entry {index} must be an object")
            _require_keys(
                value,
                _ARTIFACT_EVIDENCE_KEYS,
                label=f"conformance report artifact {direction} entry {index}",
            )
            _require_report_digest(value["digest"], f"artifact {direction} entry {index} digest")
            expected_identity = stable_urn("artifact", {"role": value["role"], "digest": value["digest"]})
            if value["identity"] != expected_identity:
                raise ConformanceError(f"conformance report artifact {direction} entry {index} identity differs")
    command = report["command"]
    if not isinstance(command["arguments"], list) or any(not isinstance(item, str) for item in command["arguments"]):
        raise ConformanceError("conformance report command arguments must be a string list")
    if command["identity"] != stable_urn("command", command["arguments"]):
        raise ConformanceError("conformance report command identity differs")
    environment = report["environment"]
    environment_content = {key: value for key, value in environment.items() if key != "identity"}
    if environment["identity"] != stable_urn("environment", environment_content):
        raise ConformanceError("conformance report environment identity differs")
    _require_report_digest(
        report["planAndConfiguration"]["configurationDigest"],
        "plan-and-configuration digest",
    )
    for name in ("counts", "dispositions"):
        if not isinstance(report["documentStores"][name], dict):
            raise ConformanceError(f"conformance report document-store {name} must be an object")
    if not isinstance(report["documentStores"]["failures"], list):
        raise ConformanceError("conformance report document-store failures must be a list")
    nonnegative_values = [
        report["documentStores"]["storeCount"],
        report["documentStores"]["retries"],
        *report["bytes"].values(),
        report["measurements"]["wallTimeMilliseconds"],
    ]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in nonnegative_values):
        raise ConformanceError("conformance report contains an invalid non-negative measurement")
    peak_memory = report["measurements"]["peakMemoryBytes"]
    if peak_memory is not None and (
        not isinstance(peak_memory, int) or isinstance(peak_memory, bool) or peak_memory < 0
    ):
        raise ConformanceError("conformance report peak-memory measurement is invalid")
    tests = report["requiredTests"]
    if not isinstance(tests, list) or not tests:
        raise ConformanceError("conformance report contains no required test evidence")
    test_ids: list[str] = []
    for index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise ConformanceError(f"conformance report required test {index} must be an object")
        _require_keys(test, _TEST_RESULT_KEYS, label=f"conformance report required test {index}")
        if test["declaredStatus"] not in {"planned", "partial", "implemented"}:
            raise ConformanceError(f"conformance report required test {index} has an unknown status")
        if test["verdict"] not in {"pass", "fail"}:
            raise ConformanceError(f"conformance report required test {index} has an unknown verdict")
        if not isinstance(test["selectors"], list) or any(not isinstance(item, str) for item in test["selectors"]):
            raise ConformanceError(f"conformance report required test {index} selectors must be a string list")
        counts = [test[name] for name in ("collected", "passed", "failed", "errors", "skipped", "xfailed")]
        measurements = [test["durationMilliseconds"], test["outputByteSize"]]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in [*counts, *measurements]):
            raise ConformanceError(f"conformance report required test {index} contains an invalid count")
        if test["collected"] != sum(test[name] for name in ("passed", "failed", "errors", "skipped")):
            raise ConformanceError(f"conformance report required test {index} counts do not reconcile")
        if test["xfailed"] > test["skipped"]:
            raise ConformanceError(f"conformance report required test {index} xfail count is invalid")
        if (test["failureCode"] is None) != (test["verdict"] == "pass"):
            raise ConformanceError(f"conformance report required test {index} failure code differs from its verdict")
        _require_report_digest(test["outputDigest"], f"required test {index} output digest")
        if test["exitCode"] is not None and (
            not isinstance(test["exitCode"], int) or isinstance(test["exitCode"], bool)
        ):
            raise ConformanceError(f"conformance report required test {index} exit code is invalid")
        if not isinstance(test["testId"], str) or not test["testId"]:
            raise ConformanceError(f"conformance report required test {index} identifier is invalid")
        test_ids.append(test["testId"])
    if len(set(test_ids)) != len(test_ids):
        raise ConformanceError("conformance report repeats a required test identifier")


def _require_report_digest(value: object, label: str) -> None:
    try:
        require_sha256(value, label)
    except ValueError as error:
        raise ConformanceError(str(error)) from error


def summarize_report(path: Path) -> dict[str, Any]:
    """Return a compact operator view of independently verified evidence."""

    report = load_report(path)
    tests = report["requiredTests"]
    passed = sum(item["verdict"] == "pass" for item in tests)
    return {
        "format": "docspec-conformance-summary",
        "formatVersion": "1.0",
        "reportId": report["reportId"],
        "specificationId": report["specificationId"],
        "conformanceClass": report["conformanceClass"],
        "requiredTestCount": len(tests),
        "passedTestCount": passed,
        "failedTestCount": len(tests) - passed,
        "firstFailureCode": report["firstFailureCode"],
        "wallTimeMilliseconds": report["measurements"]["wallTimeMilliseconds"],
        "verdict": report["verdict"],
    }
