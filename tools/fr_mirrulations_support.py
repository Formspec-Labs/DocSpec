"""Support code for the Federal Register plus Mirrulations qualification runner."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from docspec.adapters.content_fetchers import (
    AnonymousS3ContentFetcher,
    AnonymousS3ContentFetcherConfig,
    RoutingContentFetcher,
    public_s3_url,
    s3_locator,
    s3_transport_version,
)
from docspec.adapters.source_catalog import LocalFileContentFetcher, LocalJsonlSourceCatalog
from docspec.adapters.storage import sha256_file
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    identity_digest,
    parse_canonical_json,
    require_relative_path,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef
from docspec.errors import IntegrityError
from docspec.processing.json_tools import strict_json_value
from docspec.processing.segmentation import SegmentationReceipt

CAMPAIGN_ID = "fr-mirrulations-10k-v1"
FEDERAL_REGISTER_COUNT = 6_408
MIRRULATIONS_COUNT = 3_592
FULL_DOCUMENT_COUNT = 10_000
FULL_CANDIDATE_COUNT = 13_592
MIRRULATIONS_SCHEMA = "mirrulations-document-corpus-draw-v1"
MIRRULATIONS_BUCKET = "mirrulations"
MIRRULATIONS_PREFIX = "raw-data/SEC/SEC-202"
SELECTION_RULE = "sha256-source-item-id-v1"
REQUIRED_QUALIFICATION_GATES = (
    "cleanup",
    "negative-source",
    "candidate-recovery",
    "resumed-uninterrupted-equivalence",
    "release-verification",
    "repository-governance",
)
QUALIFICATION_GATE_SELECTORS = {
    "cleanup": (
        "tests/test_content_fetchers.py::test_fetch_stream_closes_unstarted_source_once",
        "tests/test_content_fetchers.py::test_local_fetcher_does_not_double_close_descriptors_under_concurrency",
        "tests/test_content_fetchers.py::test_anonymous_s3_fetcher_streams_pinned_object_and_closes",
        "tests/test_content_fetchers.py::test_anonymous_s3_fetcher_rejects_changed_response_and_closes",
        "tests/test_content_fetchers.py::test_anonymous_s3_fetcher_closes_truncated_oversized_and_failed_streams",
        "tests/test_content_fetchers.py::test_anonymous_s3_fetcher_closes_on_consumer_error_and_before_iteration",
        "tests/test_stage_checkpoint_recovery.py::test_blob_digest_rejection_closes_fetch_stream",
    ),
    "negative-source": (
        "tests/test_fr_mirrulations_qualification.py::test_translation_rejects_changed_federal_register_bytes",
        "tests/test_fr_mirrulations_qualification.py::test_translation_rejects_missing_mirrulations_pair",
        "tests/test_fr_mirrulations_qualification.py::test_execution_manifest_reconstructs_and_rejects_drift",
        "tests/test_fr_mirrulations_qualification.py::test_pinned_draw_admission_rejects_changed_draw_bytes",
        "tests/test_fr_mirrulations_qualification.py::test_pinned_draw_admission_rejects_an_altered_input_manifest",
        "tests/test_content_fetchers.py::test_anonymous_s3_fetcher_rejects_changed_response_and_closes",
        "tests/test_content_fetchers.py::test_anonymous_s3_fetcher_fails_before_io_for_bounds_and_source_escape",
        "tests/test_content_fetchers.py::test_routing_fetcher_pins_delegate_configuration_and_rejects_unknown_scheme",
    ),
    "candidate-recovery": (
        "tests/test_cli.py::test_local_run_start_resume_and_release_commit_use_real_application_services",
        "tests/test_cli.py::test_local_task_recovery_executes_only_an_unfinished_store",
        "tests/test_storage_adapters.py::test_document_store_latest_reads_only_the_newest_revision_while_revisions_validate_history",
        "tests/test_storage_adapters.py::test_revision_writes_stage_crash_debris_outside_the_declared_revision_set",
        "tests/test_storage_adapters.py::test_planned_store_ledger_presence_distinguishes_absence_from_invalid_state",
        "tests/test_fr_mirrulations_qualification.py::test_tier_run_delegates_restart_detection_to_durable_docspec_state",
        "tests/test_stage_checkpoint_recovery.py::test_two_candidate_resume_never_refetches_verified_candidates",
        "tests/test_stage_checkpoint_recovery.py::test_crash_during_second_candidate_reuses_completed_first_candidate",
        "tests/test_stage_checkpoint_recovery.py::test_hard_crash_after_capture_reuses_bytes_and_reruns_only_incomplete_extraction",
        "tests/test_stage_checkpoint_recovery.py::test_tampered_stage_receipt_fails_closed_before_any_work_restarts",
    ),
    "resumed-uninterrupted-equivalence": (
        "tests/test_stage_checkpoint_recovery.py::test_resumed_content_matches_uninterrupted_content_with_candidate_stage_revisions",
        "tests/conformance/test_incremental_equivalence.py::test_clean_incremental_targeted_and_compacted_paths_converge_on_active_document_state",
    ),
    "release-verification": (
        "tests/test_release_integrity.py::test_logical_release_verifier_accepts_complete_source_lineage",
        "tests/test_storage_records_catalog.py::test_manifest_catalog_commits_and_reopens_complete_state",
        "tests/test_fr_mirrulations_qualification.py::test_candidate_census_accepts_a_verified_zero_segment_representation",
    ),
    "repository-governance": (
        "tests/test_package_boundary.py::test_production_imports_stay_inside_the_standalone_boundary",
        "tests/test_package_boundary.py::test_core_import_and_cli_help_need_no_optional_dependency",
        "tests/test_package_boundary.py::test_built_wheel_contains_only_the_standalone_package",
        "tests/test_machine_files.py::test_module_inventory_matches_the_installed_source_tree",
        "tests/test_fr_mirrulations_qualification.py::test_gate_receipt_seals_the_tested_repository_state",
        "tests/test_fr_mirrulations_qualification.py::test_gate_runner_rejects_source_change_during_execution",
    ),
}
_FR_SCHEMA = "body-retrieval-corpus-draw-v1"
_DOCUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")


@dataclass(frozen=True, slots=True)
class QualificationTier:
    name: str
    federal_register_count: int
    mirrulations_count: int

    @property
    def document_count(self) -> int:
        return self.federal_register_count + self.mirrulations_count

    @property
    def candidate_count(self) -> int:
        return self.federal_register_count + 2 * self.mirrulations_count


TIERS = (
    QualificationTier("smoke", 64, 36),
    QualificationTier("intermediate", 641, 359),
    QualificationTier("full", FEDERAL_REGISTER_COUNT, MIRRULATIONS_COUNT),
)


@dataclass(frozen=True, slots=True)
class CorpusInputs:
    federal_register_draw: Path
    federal_register_receipts: Path
    federal_register_content: Path
    mirrulations_draw: Path


@dataclass(frozen=True, slots=True)
class FederalRegisterValidation:
    draw_digest: str
    receipt_set_digest: str
    items: tuple[SourceItem, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class MirrulationsValidation:
    draw_digest: str
    draw_id: str
    items: tuple[SourceItem, ...]
    total_bytes: int


def _read_json(path: Path, *, label: str, canonical: bool = False) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"{label} must be a regular, non-symlink file: {path}")
    payload = path.read_bytes()
    if canonical:
        value = thaw_json(parse_canonical_json(payload, label=label))
    else:
        # Deliberately not parse_closed_json. These are the producer's manifests
        # and receipts, not DocSpec artifacts: the real Federal Register draw
        # carries ten floating-point summary statistics, and DocSpec's identity
        # rules refuse every float, so identity's reader rejects the corpus
        # outright ("... .page_span.median contains a floating-point number").
        # strict_json_value is the reader for documents DocSpec did not write,
        # and it refuses the same duplicate keys and non-finite constants.
        try:
            text = payload.decode("utf-8")
        except UnicodeError as error:
            raise IntegrityError(f"{label} is not valid UTF-8: {error}") from error
        value = strict_json_value(text, label=label)
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must contain a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    """Name the digest half of the repository's one bounded file hash."""

    return sha256_file(Path(path))[0]


def _gate_evidence_files(repository: Path) -> list[dict[str, Any]]:
    repository = Path(repository).resolve(strict=True)
    excluded_parts = {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "dist",
        "output",
    }
    files = [
        path
        for path in repository.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(part in excluded_parts for part in path.relative_to(repository).parts)
    ]
    return [
        {
            "path": path.relative_to(repository).as_posix(),
            "digest": _sha256_file(path),
            "byteSize": path.stat().st_size,
        }
        for path in sorted(files)
    ]


def _pytest_evidence_command(selectors: Iterable[str] = ()) -> list[str]:
    return [
        "uv",
        "run",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--junitxml=<temporary-junit.xml>",
        *selectors,
    ]


def _run_pytest_evidence(repository: Path, selectors: tuple[str, ...] = ()) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="docspec-qualification-") as directory:
        junit = Path(directory) / "junit.xml"
        command = _pytest_evidence_command(selectors)
        actual_command = [item if item != "--junitxml=<temporary-junit.xml>" else f"--junitxml={junit}" for item in command]
        completed = subprocess.run(
            actual_command,
            cwd=repository,
            capture_output=True,
            check=False,
            text=True,
            timeout=1_200,
        )
        counts = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        if junit.is_file():
            try:
                root = ET.parse(junit).getroot()
            except (ET.ParseError, OSError) as error:
                raise IntegrityError(f"qualification pytest evidence is unreadable: {error}") from error
            for case in root.iter("testcase"):
                counts["collected"] += 1
                if case.find("failure") is not None:
                    counts["failed"] += 1
                elif case.find("error") is not None:
                    counts["errors"] += 1
                elif case.find("skipped") is not None:
                    counts["skipped"] += 1
                else:
                    counts["passed"] += 1
        verdict = (
            "passed"
            if completed.returncode == 0
            and counts["collected"] > 0
            and counts["passed"] == counts["collected"]
            else "failed"
        )
        result = {"command": command, **counts, "exitCode": completed.returncode, "verdict": verdict}
        if verdict != "passed":
            detail = (completed.stderr.strip() or completed.stdout.strip() or "pytest produced no diagnostic")[-2_000:]
            raise IntegrityError(f"qualification pytest gate failed: {detail}")
        return result


def _run_lint_evidence(repository: Path) -> dict[str, Any]:
    command = ["uv", "run", "ruff", "check", "."]
    completed = subprocess.run(
        command,
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    result = {
        "command": command,
        "exitCode": completed.returncode,
        "verdict": "passed" if completed.returncode == 0 else "failed",
    }
    if completed.returncode != 0:
        detail = (completed.stderr.strip() or completed.stdout.strip() or "ruff produced no diagnostic")[-2_000:]
        raise IntegrityError(f"qualification lint gate failed: {detail}")
    return result


def _seal_gate_receipt(
    *,
    repository: Path,
    evidence_files: list[dict[str, Any]],
    lint_result: Mapping[str, Any],
    test_result: Mapping[str, Any],
    gate_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    checks = {"lint": dict(lint_result), "tests": dict(test_result)}
    if set(gate_results) != set(REQUIRED_QUALIFICATION_GATES):
        raise IntegrityError("qualification gate results do not cover every required gate")
    sealed_gates = [
        {
            "gateId": gate_id,
            "selectors": list(QUALIFICATION_GATE_SELECTORS[gate_id]),
            "result": dict(gate_results[gate_id]),
        }
        for gate_id in REQUIRED_QUALIFICATION_GATES
    ]
    content = {
        "format": "docspec-qualification-gate-receipt",
        "formatVersion": "1.0",
        "campaignId": CAMPAIGN_ID,
        "repository": Path(repository).resolve(strict=True).as_posix(),
        "evidenceFiles": evidence_files,
        "evidenceSourceSetDigest": identity_digest(evidence_files),
        "checks": checks,
        "requiredGates": list(REQUIRED_QUALIFICATION_GATES),
        "gateResults": sealed_gates,
        "verdict": "passed",
    }
    return {**content, "gateReceiptId": stable_urn("qualification-gate-receipt", content)}


def run_qualification_gates(*, repository: Path) -> dict[str, Any]:
    """Run exact qualification gates and seal the unchanged tested source set."""

    repository = Path(repository).resolve(strict=True)
    before = _gate_evidence_files(repository)
    lint_result = _run_lint_evidence(repository)
    test_result = _run_pytest_evidence(repository)
    gate_results = {
        gate_id: _run_pytest_evidence(repository, selectors)
        for gate_id, selectors in QUALIFICATION_GATE_SELECTORS.items()
    }
    after = _gate_evidence_files(repository)
    if before != after:
        raise IntegrityError("qualification source set changed while validation gates were running")
    return _seal_gate_receipt(
        repository=repository,
        evidence_files=after,
        lint_result=lint_result,
        test_result=test_result,
        gate_results=gate_results,
    )


def validate_gate_receipt(path: Path, *, repository: Path) -> dict[str, Any]:
    receipt = _read_json(path, label="qualification gate receipt", canonical=True)
    expected_fields = {
        "format",
        "formatVersion",
        "campaignId",
        "repository",
        "evidenceFiles",
        "evidenceSourceSetDigest",
        "checks",
        "requiredGates",
        "gateResults",
        "verdict",
        "gateReceiptId",
    }
    if set(receipt) != expected_fields:
        raise IntegrityError("qualification gate receipt has an invalid closed shape")
    if (
        receipt["format"] != "docspec-qualification-gate-receipt"
        or receipt["formatVersion"] != "1.0"
        or receipt["campaignId"] != CAMPAIGN_ID
        or receipt["verdict"] != "passed"
    ):
        raise IntegrityError("qualification gate receipt did not pass")
    repository = Path(repository).resolve(strict=True)
    if receipt["repository"] != repository.as_posix():
        raise IntegrityError("qualification gate receipt names a different repository")
    evidence_files = _gate_evidence_files(repository)
    if (
        receipt["evidenceFiles"] != evidence_files
        or receipt["evidenceSourceSetDigest"] != identity_digest(evidence_files)
    ):
        raise IntegrityError("qualification gate evidence differs from the current implementation")
    identity_content = dict(receipt)
    receipt_id = identity_content.pop("gateReceiptId")
    if receipt_id != stable_urn("qualification-gate-receipt", identity_content):
        raise IntegrityError("qualification gate receipt identity differs")
    checks = receipt["checks"]
    if not isinstance(checks, dict) or set(checks) != {"lint", "tests"}:
        raise IntegrityError("qualification gate receipt checks have an invalid closed shape")
    lint = checks["lint"]
    if (
        not isinstance(lint, dict)
        or set(lint) != {"command", "exitCode", "verdict"}
        or lint["command"] != ["uv", "run", "ruff", "check", "."]
        or lint["exitCode"] != 0
        or lint["verdict"] != "passed"
    ):
        raise IntegrityError("qualification lint evidence is invalid or failed")
    _validate_pytest_evidence(checks["tests"], selectors=(), label="qualification full test suite")
    if receipt["requiredGates"] != list(REQUIRED_QUALIFICATION_GATES):
        raise IntegrityError("qualification gate receipt does not cover every required gate")
    gate_results = receipt["gateResults"]
    if not isinstance(gate_results, list) or len(gate_results) != len(REQUIRED_QUALIFICATION_GATES):
        raise IntegrityError("qualification per-gate evidence has an invalid shape")
    for index, gate_id in enumerate(REQUIRED_QUALIFICATION_GATES):
        gate = gate_results[index]
        selectors = QUALIFICATION_GATE_SELECTORS[gate_id]
        if (
            not isinstance(gate, dict)
            or set(gate) != {"gateId", "selectors", "result"}
            or gate["gateId"] != gate_id
            or gate["selectors"] != list(selectors)
            or not selectors
        ):
            raise IntegrityError("qualification per-gate selector evidence differs")
        _validate_pytest_evidence(gate["result"], selectors=selectors, label=f"qualification gate {gate_id}")
    return receipt


def _validate_pytest_evidence(value: Any, *, selectors: tuple[str, ...], label: str) -> None:
    fields = {"command", "collected", "passed", "failed", "errors", "skipped", "exitCode", "verdict"}
    if not isinstance(value, dict) or set(value) != fields:
        raise IntegrityError(f"{label} evidence has an invalid closed shape")
    counts = {name: value[name] for name in ("collected", "passed", "failed", "errors", "skipped")}
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts.values()):
        raise IntegrityError(f"{label} evidence contains an invalid count")
    if (
        value["command"] != _pytest_evidence_command(selectors)
        or value["exitCode"] != 0
        or value["verdict"] != "passed"
        or counts["collected"] < max(1, len(selectors))
        or counts["passed"] != counts["collected"]
        or counts["failed"] != 0
        or counts["errors"] != 0
        or counts["skipped"] != 0
    ):
        raise IntegrityError(f"{label} evidence is incomplete or failed")


def _regular_directory(path: Path, *, label: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise IntegrityError(f"{label} must be an existing, non-symlink directory: {path}")
    return path.resolve(strict=True)


def _stable_selection(items: Iterable[SourceItem], count: int) -> tuple[SourceItem, ...]:
    ranked = sorted(
        items,
        key=lambda item: (hashlib.sha256(item.item_id.encode("utf-8")).hexdigest(), item.item_id),
    )
    if len(ranked) < count:
        raise IntegrityError(f"source population has {len(ranked)} items but the tier requires {count}")
    return tuple(ranked[:count])


def _federal_register_transport_version(receipt: Mapping[str, Any]) -> str:
    return stable_urn(
        "http-transport-version",
        {
            "format": "docspec-qualification-http-transport-version",
            "formatVersion": "1.0",
            "sourceUrl": receipt["source_url"],
            "resolvedUrl": receipt["resolved_url"],
            "etag": receipt["etag"],
            "lastModified": receipt["last_modified"],
        },
    )


def validate_federal_register(inputs: CorpusInputs) -> FederalRegisterValidation:
    """Verify all retained Federal Register bytes and translate them to SourceItems."""

    draw_path = Path(inputs.federal_register_draw).resolve(strict=True)
    receipt_root = _regular_directory(inputs.federal_register_receipts, label="Federal Register receipt root")
    content_root = _regular_directory(inputs.federal_register_content, label="Federal Register content root")
    draw = _read_json(draw_path, label="Federal Register final draw")
    if draw.get("schema_version") != _FR_SCHEMA:
        raise IntegrityError("Federal Register draw schema differs")
    documents = draw.get("documents")
    if not isinstance(documents, list) or len(documents) != FULL_DOCUMENT_COUNT:
        raise IntegrityError("Federal Register final draw must contain exactly 10,000 documents")
    by_id: dict[str, dict[str, Any]] = {}
    for value in documents:
        if not isinstance(value, dict):
            raise IntegrityError("Federal Register draw document must be an object")
        document_id = value.get("document_number")
        if not isinstance(document_id, str) or not document_id:
            raise IntegrityError("Federal Register draw contains an invalid document number")
        if document_id in by_id:
            raise IntegrityError("Federal Register draw repeats a document number")
        by_id[document_id] = value

    receipt_paths = sorted(receipt_root.glob("*.json"))
    content_paths = sorted(content_root.glob("*.xml"))
    if len(receipt_paths) != FEDERAL_REGISTER_COUNT or len(content_paths) != FEDERAL_REGISTER_COUNT:
        raise IntegrityError("Federal Register retained cache must contain exactly 6,408 receipts and XML files")
    content_names = {path.name for path in content_paths}
    if len(content_names) != FEDERAL_REGISTER_COUNT:
        raise IntegrityError("Federal Register retained cache repeats an XML file name")

    receipt_evidence: list[dict[str, str]] = []
    items: list[SourceItem] = []
    seen_documents: set[str] = set()
    total_bytes = 0
    for receipt_path in receipt_paths:
        receipt = _read_json(receipt_path, label=f"Federal Register receipt {receipt_path.name}")
        required = {
            "cache_file",
            "document_number",
            "etag",
            "last_modified",
            "resolved_url",
            "retrieved_on",
            "source_bytes",
            "source_sha256",
            "source_url",
            "status",
        }
        if not required <= set(receipt) or receipt.get("status") != "ok":
            raise IntegrityError(f"Federal Register receipt is incomplete: {receipt_path.name}")
        document_id = require_text(receipt["document_number"], "Federal Register receipt document number")
        if document_id in seen_documents or document_id not in by_id:
            raise IntegrityError("Federal Register receipt membership differs from the final draw")
        seen_documents.add(document_id)
        locator = require_relative_path(receipt["cache_file"], "Federal Register receipt cache file")
        if PurePosixPath(locator).parent != PurePosixPath(".") or locator not in content_names:
            raise IntegrityError("Federal Register receipt cache file differs from the sealed content root")
        source_size = receipt["source_bytes"]
        source_hash = receipt["source_sha256"]
        if isinstance(source_size, bool) or not isinstance(source_size, int) or source_size < 0:
            raise IntegrityError("Federal Register receipt has an invalid source byte count")
        if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            raise IntegrityError("Federal Register receipt has an invalid source SHA-256")
        content_path = content_root / locator
        if content_path.is_symlink() or not content_path.is_file():
            raise IntegrityError("Federal Register receipt does not resolve to a regular XML file")
        payload_digest = _sha256_file(content_path)
        if content_path.stat().st_size != source_size or payload_digest != f"sha256:{source_hash}":
            raise IntegrityError(f"Federal Register XML differs from its receipt: {document_id}")
        receipt_digest = _sha256_file(receipt_path)
        receipt_evidence.append({"path": receipt_path.name, "digest": receipt_digest})
        total_bytes += source_size
        draw_metadata = by_id[document_id]
        metadata = {
            "qualification": {
                "campaignId": CAMPAIGN_ID,
                "source": "federal-register",
                "finalDraw": draw_metadata,
                "receiptRef": f"receipts/{receipt_path.name}",
                "sourceUrl": receipt["source_url"],
                "resolvedUrl": receipt["resolved_url"],
                "retrievedOn": receipt["retrieved_on"],
                "etag": receipt["etag"],
                "lastModified": receipt["last_modified"],
            },
            "estimatedBytes": source_size,
            "expectedSegments": 1_000,
            "processorCost": 1_000,
            "estimatedDurationSeconds": 10,
        }
        items.append(
            SourceItem(
                f"urn:docspec:qualification:federal-register:{document_id}",
                f"sha256:{source_hash}",
                (
                    CandidateFile(
                        "federal-register-xml",
                        locator,
                        "text/xml",
                        expected_digest=f"sha256:{source_hash}",
                        expected_size=source_size,
                        transport_version=_federal_register_transport_version(receipt),
                        metadata={
                            "sourceUrl": receipt["source_url"],
                            "resolvedUrl": receipt["resolved_url"],
                            "receiptRef": f"receipts/{receipt_path.name}",
                        },
                    ),
                ),
                metadata=metadata,
            )
        )
    if len(seen_documents) != FEDERAL_REGISTER_COUNT:
        raise IntegrityError("Federal Register retained membership is incomplete")
    receipt_set_digest = identity_digest(
        {
            "format": "docspec-qualification-federal-register-receipt-set",
            "formatVersion": "1.0",
            "receipts": receipt_evidence,
        }
    )
    return FederalRegisterValidation(
        _sha256_file(draw_path),
        receipt_set_digest,
        tuple(sorted(items, key=lambda item: (item.item_id, item.version))),
        total_bytes,
    )


def _mirrulations_object(
    value: object,
    *,
    label: str,
    bucket: str,
    prefix: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"key", "size", "etag", "last_modified"}:
        raise IntegrityError(f"{label} has an invalid closed shape")
    key = require_relative_path(value["key"], f"{label} key")
    size = value["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise IntegrityError(f"{label} has an invalid size")
    if not key.startswith(prefix):
        raise IntegrityError(f"{label} is outside the Mirrulations prefix")
    return {
        "bucket": bucket,
        "key": key,
        "size": size,
        "etag": require_text(value["etag"], f"{label} ETag"),
        "lastModified": require_text(value["last_modified"], f"{label} last-modified time"),
    }


def _mirrulations_draw_id(draw: Mapping[str, Any]) -> str:
    semantic = dict(draw)
    semantic.pop("draw_id", None)
    digest = hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()
    return f"urn:spicyregs:mirrulations-document-draw:{digest[:24]}"


def validate_mirrulations_draw(inputs: CorpusInputs) -> MirrulationsValidation:
    """Validate a SpicyRegs-produced draw and translate both required objects."""

    path = Path(inputs.mirrulations_draw).resolve(strict=True)
    draw = _read_json(path, label="SpicyRegs Mirrulations draw")
    if draw.get("schema_version") != MIRRULATIONS_SCHEMA:
        raise IntegrityError("Mirrulations draw schema differs")
    if draw.get("draw_id") != _mirrulations_draw_id(draw):
        raise IntegrityError("Mirrulations draw identity differs from its canonical content")
    source = draw.get("source")
    if source != {"bucket": MIRRULATIONS_BUCKET, "prefix": MIRRULATIONS_PREFIX}:
        raise IntegrityError("Mirrulations draw source boundary differs")
    selection = draw.get("selection")
    if not isinstance(selection, dict) or selection.get("max_documents") != MIRRULATIONS_COUNT:
        raise IntegrityError("Mirrulations draw selection does not require exactly 3,592 documents")
    documents = draw.get("documents")
    if not isinstance(documents, list) or len(documents) != MIRRULATIONS_COUNT:
        raise IntegrityError("Mirrulations draw must contain exactly 3,592 documents")

    items: list[SourceItem] = []
    seen: set[str] = set()
    total_bytes = 0
    for entry in documents:
        if not isinstance(entry, dict):
            raise IntegrityError("Mirrulations draw document must be an object")
        document_id = entry.get("document_id")
        if not isinstance(document_id, str) or _DOCUMENT_ID.fullmatch(document_id) is None:
            raise IntegrityError("Mirrulations draw contains an invalid document ID")
        if document_id in seen:
            raise IntegrityError("Mirrulations draw repeats a document ID")
        seen.add(document_id)
        metadata_object = _mirrulations_object(
            entry.get("metadata_object"),
            label=f"Mirrulations metadata object {document_id}",
            bucket=MIRRULATIONS_BUCKET,
            prefix=MIRRULATIONS_PREFIX,
        )
        rendition_object = _mirrulations_object(
            entry.get("rendition_object"),
            label=f"Mirrulations rendition object {document_id}",
            bucket=MIRRULATIONS_BUCKET,
            prefix=MIRRULATIONS_PREFIX,
        )
        metadata_name = PurePosixPath(metadata_object["key"]).name
        rendition_path = PurePosixPath(rendition_object["key"])
        if not metadata_name.endswith(".json") or rendition_path.suffix.casefold() not in {".htm", ".html"}:
            raise IntegrityError("Mirrulations draw document does not contain one JSON and one HTML object")
        if "/documents/" not in metadata_object["key"] or "/documents/" not in rendition_object["key"]:
            raise IntegrityError("Mirrulations draw selected an object outside a documents directory")
        version_content = {
            "format": "docspec-qualification-mirrulations-source-version",
            "formatVersion": "1.0",
            "documentId": document_id,
            "metadataObject": metadata_object,
            "renditionObject": rendition_object,
        }
        total_size = metadata_object["size"] + rendition_object["size"]
        total_bytes += total_size

        def candidate(candidate_id: str, media_type: str, record: dict[str, Any]) -> CandidateFile:
            return CandidateFile(
                candidate_id,
                s3_locator(record["bucket"], record["key"]),
                media_type,
                expected_size=record["size"],
                transport_version=s3_transport_version(
                    bucket=record["bucket"],
                    key=record["key"],
                    size=record["size"],
                    etag=record["etag"],
                    last_modified=record["lastModified"],
                ),
                metadata={
                    "s3": record,
                    "publicSourceUrl": public_s3_url(
                        bucket=record["bucket"],
                        key=record["key"],
                        region_name="us-east-1",
                    ),
                },
            )

        items.append(
            SourceItem(
                f"urn:docspec:qualification:mirrulations:{document_id}",
                sha256_digest(canonical_json_bytes(version_content)),
                (
                    candidate("metadata-json", "application/json", metadata_object),
                    candidate("rendition-html", "text/html", rendition_object),
                ),
                metadata={
                    "qualification": {
                        "campaignId": CAMPAIGN_ID,
                        "source": "mirrulations",
                        "drawId": draw["draw_id"],
                        "documentId": document_id,
                        "jsonRevision": entry.get("json_revision"),
                        "mirrorDirectory": entry.get("mirror_directory"),
                    },
                    "estimatedBytes": total_size,
                    "expectedSegments": 1_000,
                    "processorCost": 1_000,
                    "estimatedDurationSeconds": 10,
                },
            )
        )
    counts = draw.get("counts")
    if not isinstance(counts, dict) or counts.get("selected_documents") != MIRRULATIONS_COUNT:
        raise IntegrityError("Mirrulations draw counts differ from its selected population")
    return MirrulationsValidation(
        _sha256_file(path),
        require_text(draw["draw_id"], "Mirrulations draw ID"),
        tuple(sorted(items, key=lambda item: (item.item_id, item.version))),
        total_bytes,
    )


def build_source_items(inputs: CorpusInputs) -> tuple[FederalRegisterValidation, MirrulationsValidation]:
    federal_register = validate_federal_register(inputs)
    mirrulations = validate_mirrulations_draw(inputs)
    item_ids = {item.item_id for item in (*federal_register.items, *mirrulations.items)}
    if len(item_ids) != FULL_DOCUMENT_COUNT:
        raise IntegrityError("combined qualification corpus does not contain 10,000 unique item identities")
    return federal_register, mirrulations


def build_catalogs(
    inputs: CorpusInputs,
    *,
    catalog_root: Path,
) -> tuple[dict[str, SourceCatalogRef], dict[str, Any]]:
    """Write all three deterministic catalogs from the same verified producer inputs."""

    federal_register, mirrulations = build_source_items(inputs)
    writer = LocalJsonlSourceCatalog(catalog_root)
    references: dict[str, SourceCatalogRef] = {}
    summaries: dict[str, Any] = {}
    for tier in TIERS:
        selected_fr = _stable_selection(federal_register.items, tier.federal_register_count)
        selected_mirr = _stable_selection(mirrulations.items, tier.mirrulations_count)
        members = tuple(sorted((*selected_fr, *selected_mirr), key=lambda item: (item.item_id, item.version)))
        candidate_count = sum(len(item.candidates) for item in members)
        if len(members) != tier.document_count or candidate_count != tier.candidate_count:
            raise IntegrityError(f"{tier.name} catalog composition differs from its sealed tier")
        media_counts = Counter(candidate.media_type for item in members for candidate in item.candidates)
        member_ids = [item.item_id for item in members]
        coverage = {
            "format": "docspec-qualification-catalog-coverage",
            "formatVersion": "1.0",
            "campaignId": CAMPAIGN_ID,
            "tier": tier.name,
            "parentInputs": {
                "federalRegisterDraw": {
                    "path": Path(inputs.federal_register_draw).resolve(strict=True).as_posix(),
                    "digest": federal_register.draw_digest,
                    "receiptSetDigest": federal_register.receipt_set_digest,
                },
                "mirrulationsDraw": {
                    "path": Path(inputs.mirrulations_draw).resolve(strict=True).as_posix(),
                    "digest": mirrulations.draw_digest,
                    "drawId": mirrulations.draw_id,
                },
            },
            "selection": {"rule": SELECTION_RULE, "orderedMemberIds": member_ids},
            "counts": {
                "documents": len(members),
                "candidates": candidate_count,
                "sources": {
                    "federal-register": len(selected_fr),
                    "mirrulations": len(selected_mirr),
                },
                "mediaTypes": dict(sorted(media_counts.items())),
            },
        }
        reference = writer.write(members, coverage=coverage)
        writer.verify(reference)
        references[tier.name] = reference
        summaries[tier.name] = {**coverage, "sourceCatalog": reference.to_dict()}
    return references, {
        "format": "docspec-qualification-catalog-set",
        "formatVersion": "1.0",
        "campaignId": CAMPAIGN_ID,
        "inputs": {
            "federalRegister": {
                "documents": len(federal_register.items),
                "bytes": federal_register.total_bytes,
                "drawDigest": federal_register.draw_digest,
                "receiptSetDigest": federal_register.receipt_set_digest,
            },
            "mirrulations": {
                "documents": len(mirrulations.items),
                "candidates": sum(len(item.candidates) for item in mirrulations.items),
                "bytes": mirrulations.total_bytes,
                "drawDigest": mirrulations.draw_digest,
                "drawId": mirrulations.draw_id,
            },
        },
        "tiers": summaries,
    }


def require_producer_identity(value: Any) -> dict[str, Any]:
    """Return the producer facts the pinned draw carries, without touching a checkout.

    Every field arrives from the tracked input manifest that pins the draw
    bytes. `builderPath` names a file inside the producer's own repository and
    is deliberately never resolved here: the draw's provenance is a recorded
    fact about bytes already published, not a path this repository can visit.
    """

    if not isinstance(value, Mapping) or set(value) != {
        "name",
        "commit",
        "builderPath",
        "builderDigest",
        "schema",
    }:
        raise IntegrityError("Mirrulations producer identity has an invalid closed shape")
    if value["schema"] != MIRRULATIONS_SCHEMA:
        raise IntegrityError("Mirrulations producer identity names an unknown draw schema")
    return {
        "name": require_text(value["name"], "Mirrulations producer name"),
        "commit": require_text(value["commit"], "Mirrulations producer commit"),
        "builderPath": require_relative_path(value["builderPath"], "Mirrulations builder path"),
        "builderDigest": require_sha256(value["builderDigest"], "Mirrulations builder digest"),
        "schema": MIRRULATIONS_SCHEMA,
    }


def build_execution_manifest(
    *,
    tier: QualificationTier,
    inputs: CorpusInputs,
    federal_register: FederalRegisterValidation,
    mirrulations: MirrulationsValidation,
    source_catalog: SourceCatalogRef,
    processing_plan_path: Path,
    processing_plan_id: str,
    run_request_path: Path,
    output_roots: Mapping[str, Path],
    mirrulations_producer: Mapping[str, Any],
    runner_path: Path,
    runner_support_path: Path,
    gate_receipt_path: Path,
    workers: int,
    max_object_bytes: int,
    retry_policy: Mapping[str, Any],
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    """Create one identity-bearing execution manifest for a qualification tier."""

    if workers <= 0 or max_object_bytes <= 0 or chunk_size <= 0:
        raise ValueError("qualification execution bounds must be positive")
    local = LocalFileContentFetcher(inputs.federal_register_content, chunk_size=chunk_size)
    s3_config = AnonymousS3ContentFetcherConfig(
        MIRRULATIONS_BUCKET,
        MIRRULATIONS_PREFIX,
        chunk_size=chunk_size,
        max_pool_connections=max(workers, 4),
    )
    # The routing identity depends only on the delegate identities and configurations.
    class _IdentityOnlyS3:
        downloader_id = AnonymousS3ContentFetcher.downloader_id
        configuration_digest = s3_config.digest

        def fetch(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("identity-only S3 fetcher cannot perform I/O")

    routing = RoutingContentFetcher(local=local, s3=_IdentityOnlyS3(), max_object_bytes=max_object_bytes)
    content = {
        "format": "qualification-execution-manifest-v1",
        "formatVersion": "1.0",
        "campaignId": CAMPAIGN_ID,
        "tier": tier.name,
        "producerInputs": {
            "federalRegister": {
                "draw": {
                    "path": Path(inputs.federal_register_draw).resolve(strict=True).as_posix(),
                    "digest": federal_register.draw_digest,
                },
                "receiptRoot": Path(inputs.federal_register_receipts).resolve(strict=True).as_posix(),
                "receiptSetDigest": federal_register.receipt_set_digest,
                "contentRoot": Path(inputs.federal_register_content).resolve(strict=True).as_posix(),
                "documentCount": FEDERAL_REGISTER_COUNT,
                "totalBytes": federal_register.total_bytes,
            },
            "mirrulations": {
                "draw": {
                    "path": Path(inputs.mirrulations_draw).resolve(strict=True).as_posix(),
                    "digest": mirrulations.draw_digest,
                    "drawId": mirrulations.draw_id,
                },
                "producer": require_producer_identity(mirrulations_producer),
                "documentCount": MIRRULATIONS_COUNT,
                "candidateCount": 2 * MIRRULATIONS_COUNT,
                "totalBytes": mirrulations.total_bytes,
            },
        },
        "acquisition": {
            "routing": {
                "implementationId": routing.downloader_id,
                "configurationDigest": routing.configuration_digest,
                "rules": [
                    {"locator": "relative-path", "delegate": local.downloader_id},
                    {"locator": "s3", "delegate": AnonymousS3ContentFetcher.downloader_id},
                ],
            },
            "local": {
                "implementationId": local.downloader_id,
                "configurationDigest": local.configuration_digest,
                "root": local.root.as_posix(),
                "chunkSize": chunk_size,
            },
            "s3": {
                "implementationId": AnonymousS3ContentFetcher.downloader_id,
                "configurationDigest": s3_config.digest,
                **s3_config.identity_content(),
            },
            "maximumObjectBytes": max_object_bytes,
        },
        "execution": {
            "workerCount": workers,
            "retryPolicy": dict(retry_policy),
        },
        "processingPlan": {
            "path": Path(processing_plan_path).resolve(strict=True).as_posix(),
            "digest": _sha256_file(processing_plan_path),
            "planId": processing_plan_id,
        },
        "sourceCatalog": source_catalog.to_dict(),
        "runRequest": {
            "path": Path(run_request_path).resolve(strict=True).as_posix(),
            "digest": _sha256_file(run_request_path),
        },
        "outputRoots": {name: Path(path).resolve(strict=True).as_posix() for name, path in sorted(output_roots.items())},
        "runner": {
            "implementationId": "docspec.qualification.fr-mirrulations-runner.v1",
            "path": Path(runner_path).resolve(strict=True).as_posix(),
            "digest": _sha256_file(runner_path),
            "supportPath": Path(runner_support_path).resolve(strict=True).as_posix(),
            "supportDigest": _sha256_file(runner_support_path),
        },
        "qualificationGates": {
            "path": Path(gate_receipt_path).resolve(strict=True).as_posix(),
            "digest": _sha256_file(gate_receipt_path),
            "gateReceiptId": validate_gate_receipt(
                gate_receipt_path,
                repository=Path(runner_path).resolve(strict=True).parents[1],
            )["gateReceiptId"],
        },
    }
    return {**content, "manifestId": stable_urn("qualification-execution-manifest", content)}


def validate_execution_manifest(path: Path) -> dict[str, Any]:
    """Verify a manifest and every local identity it seals before source I/O."""

    manifest = _read_json(path, label="qualification execution manifest", canonical=True)
    expected_fields = {
        "format",
        "formatVersion",
        "campaignId",
        "tier",
        "producerInputs",
        "acquisition",
        "execution",
        "processingPlan",
        "sourceCatalog",
        "runRequest",
        "outputRoots",
        "runner",
        "qualificationGates",
        "manifestId",
    }
    if set(manifest) != expected_fields:
        raise IntegrityError("qualification execution manifest has an invalid closed shape")
    if (
        manifest["format"] != "qualification-execution-manifest-v1"
        or manifest["formatVersion"] != "1.0"
        or manifest["campaignId"] != CAMPAIGN_ID
    ):
        raise IntegrityError("qualification execution manifest has an unknown format")
    identity_content = dict(manifest)
    manifest_id = identity_content.pop("manifestId")
    if manifest_id != stable_urn("qualification-execution-manifest", identity_content):
        raise IntegrityError("qualification execution manifest identity differs")
    tier_name = manifest["tier"]
    if tier_name not in {tier.name for tier in TIERS}:
        raise IntegrityError("qualification execution manifest names an unknown tier")
    producer = manifest["producerInputs"]
    if not isinstance(producer, dict) or set(producer) != {"federalRegister", "mirrulations"}:
        raise IntegrityError("qualification producer inputs have an invalid closed shape")
    federal_register = producer["federalRegister"]
    mirrulations = producer["mirrulations"]
    # Each draw's bytes are hashed once below, where `build_source_items`
    # rereads them and the recomputed digest is compared against this manifest.
    # Hashing them again here repeated the work without adding a guarantee.
    for label, value in (("Federal Register", federal_register), ("Mirrulations", mirrulations)):
        if not isinstance(value, dict) or not isinstance(value.get("draw"), dict):
            raise IntegrityError(f"{label} manifest input is invalid")
        require_sha256(value["draw"]["digest"], f"{label} draw digest")
    require_producer_identity(mirrulations.get("producer"))
    for name in ("processingPlan", "runRequest", "runner"):
        artifact = manifest[name]
        if not isinstance(artifact, dict) or _sha256_file(Path(artifact["path"])) != require_sha256(
            artifact["digest"], f"{name} digest"
        ):
            raise IntegrityError(f"{name} differs from the execution manifest")
    runner = manifest["runner"]
    if _sha256_file(Path(runner["supportPath"])) != require_sha256(
        runner["supportDigest"], "runner support digest"
    ):
        raise IntegrityError("runner support differs from the execution manifest")
    gate_spec = manifest["qualificationGates"]
    if not isinstance(gate_spec, dict) or set(gate_spec) != {"path", "digest", "gateReceiptId"}:
        raise IntegrityError("qualification gate reference has an invalid closed shape")
    gate_path = Path(gate_spec["path"])
    if _sha256_file(gate_path) != require_sha256(gate_spec["digest"], "qualification gate digest"):
        raise IntegrityError("qualification gate receipt differs from the execution manifest")
    gate_receipt = validate_gate_receipt(gate_path, repository=Path(manifest["runner"]["path"]).parents[1])
    if gate_receipt["gateReceiptId"] != gate_spec["gateReceiptId"]:
        raise IntegrityError("qualification gate receipt identity differs from the execution manifest")
    federal_spec = federal_register
    mirrulations_spec = mirrulations
    current_federal, current_mirrulations = build_source_items(
        CorpusInputs(
            Path(federal_spec["draw"]["path"]),
            Path(federal_spec["receiptRoot"]),
            Path(federal_spec["contentRoot"]),
            Path(mirrulations_spec["draw"]["path"]),
        )
    )
    if (
        current_federal.draw_digest != federal_spec["draw"]["digest"]
        or current_federal.receipt_set_digest != federal_spec["receiptSetDigest"]
        or len(current_federal.items) != federal_spec["documentCount"]
        or current_federal.total_bytes != federal_spec["totalBytes"]
    ):
        raise IntegrityError("Federal Register producer inputs differ from the execution manifest")
    if (
        current_mirrulations.draw_digest != mirrulations_spec["draw"]["digest"]
        or current_mirrulations.draw_id != mirrulations_spec["draw"]["drawId"]
        or len(current_mirrulations.items) != mirrulations_spec["documentCount"]
        or sum(len(item.candidates) for item in current_mirrulations.items) != mirrulations_spec["candidateCount"]
        or current_mirrulations.total_bytes != mirrulations_spec["totalBytes"]
    ):
        raise IntegrityError("Mirrulations producer inputs differ from the execution manifest")
    for root in manifest["outputRoots"].values():
        _regular_directory(Path(root), label="qualification output root")
    reconstruct_fetcher(manifest, identity_only=True)
    return manifest


def reconstruct_fetcher(
    manifest: Mapping[str, Any],
    *,
    s3_client: Any | None = None,
    identity_only: bool = False,
) -> RoutingContentFetcher:
    """Rebuild the exact mixed fetcher and reject any configuration drift."""

    acquisition = manifest.get("acquisition")
    if not isinstance(acquisition, Mapping) or set(acquisition) != {
        "routing",
        "local",
        "s3",
        "maximumObjectBytes",
    }:
        raise IntegrityError("qualification acquisition composition has an invalid closed shape")
    local_spec = acquisition["local"]
    s3_spec = acquisition["s3"]
    routing_spec = acquisition["routing"]
    if not all(isinstance(value, Mapping) for value in (local_spec, s3_spec, routing_spec)):
        raise IntegrityError("qualification fetcher composition is invalid")
    local = LocalFileContentFetcher(Path(local_spec["root"]), chunk_size=local_spec["chunkSize"])
    config_fields = {
        "bucket": s3_spec["bucket"],
        "prefix": s3_spec["prefix"],
        "region_name": s3_spec["regionName"],
        "chunk_size": s3_spec["chunkSize"],
        "connect_timeout_seconds": s3_spec["connectTimeoutSeconds"],
        "read_timeout_seconds": s3_spec["readTimeoutSeconds"],
        "sdk_max_attempts": s3_spec["sdkMaxAttempts"],
        "max_pool_connections": s3_spec["maxPoolConnections"],
        "anonymous": s3_spec["anonymous"],
    }
    config = AnonymousS3ContentFetcherConfig(**config_fields)

    class _IdentityOnlyS3:
        downloader_id = AnonymousS3ContentFetcher.downloader_id
        configuration_digest = config.digest

        def fetch(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("identity-only S3 fetcher cannot perform I/O")

    if identity_only:
        s3: Any = _IdentityOnlyS3()
    elif s3_client is None:
        s3 = AnonymousS3ContentFetcher.from_boto3(config)
    else:
        s3 = AnonymousS3ContentFetcher(s3_client, config)
    maximum_object_bytes = acquisition["maximumObjectBytes"]
    if (
        isinstance(maximum_object_bytes, bool)
        or not isinstance(maximum_object_bytes, int)
        or maximum_object_bytes <= 0
    ):
        raise IntegrityError("qualification maximum object bytes is invalid")
    routing = RoutingContentFetcher(local=local, s3=s3, max_object_bytes=maximum_object_bytes)
    expected = (
        (local.downloader_id, local.configuration_digest, local_spec),
        (s3.downloader_id, s3.configuration_digest, s3_spec),
        (routing.downloader_id, routing.configuration_digest, routing_spec),
    )
    for implementation_id, configuration_digest, sealed in expected:
        if (
            sealed.get("implementationId") != implementation_id
            or sealed.get("configurationDigest") != configuration_digest
        ):
            raise IntegrityError("reconstructed content fetcher differs from the execution manifest")
    if config.bucket != MIRRULATIONS_BUCKET or config.prefix != MIRRULATIONS_PREFIX or config.anonymous is not True:
        raise IntegrityError("qualification S3 boundary differs from the campaign")
    return routing


def build_candidate_census(
    *,
    tier: str,
    source_catalog: LocalJsonlSourceCatalog,
    source_catalog_ref: SourceCatalogRef,
    document_catalog: Any,
    controls: Any,
    release_ref: DocumentReleaseRef,
    gate_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconcile every required candidate to one final campaign status."""

    reader = document_catalog.open_reader(release_ref)
    release = reader.release
    items = tuple(source_catalog.stream(source_catalog_ref))
    files_by_source: dict[str, dict[str, dict[str, Any]]] = {}
    for row in reader.scan(layer_kind="files"):
        payload = row.get("payload")
        source_item_id = row.get("sourceItemId")
        if not isinstance(payload, dict) or not isinstance(source_item_id, str):
            raise IntegrityError("release file layer contains an invalid candidate record")
        candidate_id = payload.get("candidateId")
        if not isinstance(candidate_id, str):
            raise IntegrityError("release file record has no candidate identity")
        current = files_by_source.setdefault(source_item_id, {})
        if candidate_id in current:
            raise IntegrityError("release contains more than one capture for a source candidate")
        current[candidate_id] = payload
    representations_by_file: dict[str, dict[str, Any]] = {}
    for row in reader.scan(layer_kind="representations"):
        payload = row.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("fileId"), str):
            raise IntegrityError("release representation layer contains an invalid record")
        file_id = payload["fileId"]
        if file_id in representations_by_file:
            raise IntegrityError("release contains more than one representation for a captured file")
        representations_by_file[file_id] = payload
    segment_ids_by_representation: dict[str, set[str]] = {}
    for row in reader.scan(layer_kind="segments"):
        payload = row.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("representationId"), str):
            raise IntegrityError("release segment layer contains an invalid record")
        segment_ids_by_representation.setdefault(payload["representationId"], set()).add(payload["segmentId"])
    segmented_representations: set[str] = set()
    for row in reader.scan(layer_kind="receipts"):
        payload = row.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("artifact"), dict):
            raise IntegrityError("release receipt layer contains an invalid record")
        reference = ArtifactRef.from_dict(payload["artifact"])
        value = controls.load(reference)
        if value.get("format") != "docspec-segmentation-receipt":
            continue
        receipt = SegmentationReceipt.from_dict(value)
        if receipt.representation_id in segmented_representations:
            raise IntegrityError("release repeats a segmentation receipt for one representation")
        if set(receipt.segment_ids) != segment_ids_by_representation.get(receipt.representation_id, set()):
            raise IntegrityError("segmentation receipt differs from the release segment population")
        segmented_representations.add(receipt.representation_id)
    dispositions: dict[str, str] = {}
    for row in reader.scan(layer_kind="dispositions"):
        source_item_id = row.get("sourceItemId")
        payload = row.get("payload")
        if not isinstance(source_item_id, str) or not isinstance(payload, dict):
            raise IntegrityError("release disposition layer contains an invalid record")
        disposition = payload.get("disposition")
        if not isinstance(disposition, str) or source_item_id in dispositions:
            raise IntegrityError("release does not contain exactly one terminal disposition per source item")
        dispositions[source_item_id] = disposition

    status_counts = Counter(
        {"processed": 0, "acquisition-failed": 0, "processing-failed": 0, "not-attempted": 0}
    )
    source_counts: Counter[str] = Counter()
    media_counts: Counter[str] = Counter()
    required_bytes = 0
    candidate_rows: list[dict[str, Any]] = []
    expected_item_ids = {item.item_id for item in items}
    if set(dispositions) != expected_item_ids:
        raise IntegrityError("release terminal dispositions differ from the source catalog population")
    for item in items:
        qualification_metadata = item.metadata.get("qualification", {})
        source_name = qualification_metadata.get("source", "unknown")
        source_counts[source_name] += 1
        candidates = files_by_source.get(item.item_id, {})
        earlier_failed = False
        for candidate in item.candidates:
            captured = candidates.get(candidate.candidate_id)
            if earlier_failed:
                status = "not-attempted"
            elif captured is None:
                status = "acquisition-failed"
                earlier_failed = True
            else:
                representation = representations_by_file.get(captured["fileId"])
                if representation is None or representation.get("representationId") not in segmented_representations:
                    status = "processing-failed"
                    earlier_failed = True
                else:
                    status = "processed"
            status_counts[status] += 1
            media_counts[candidate.media_type] += 1
            if candidate.expected_size is None:
                raise IntegrityError("qualification candidate has no sealed byte count")
            required_bytes += candidate.expected_size
            candidate_rows.append(
                {
                    "sourceItemId": item.item_id,
                    "sourceVersion": item.version,
                    "candidateId": candidate.candidate_id,
                    "mediaType": candidate.media_type,
                    "expectedBytes": candidate.expected_size,
                    "status": status,
                }
            )
    expected_candidate_count = sum(len(item.candidates) for item in items)
    if len(candidate_rows) != expected_candidate_count or sum(status_counts.values()) != expected_candidate_count:
        raise IntegrityError("candidate census does not balance to the required catalog population")
    disposition_counts = Counter(dispositions.values())
    if gate_receipt.get("verdict") != "passed" or not isinstance(gate_receipt.get("gateReceiptId"), str):
        raise IntegrityError("qualification gates did not pass")
    passed = (
        status_counts["processed"] == expected_candidate_count
        and len(items) == len(dispositions)
        and disposition_counts == {"captured": len(items)}
        and gate_receipt["verdict"] == "passed"
    )
    return {
        "format": "docspec-qualification-candidate-census",
        "formatVersion": "1.0",
        "campaignId": CAMPAIGN_ID,
        "tier": tier,
        "sourceCatalog": source_catalog_ref.to_dict(),
        "release": release_ref.to_dict(),
        "logicalStateDigest": release.logical_state_digest,
        "counts": {
            "documents": len(items),
            "requiredCandidates": expected_candidate_count,
            "requiredBytes": required_bytes,
            "sources": dict(sorted(source_counts.items())),
            "mediaTypes": dict(sorted(media_counts.items())),
            "candidateStatuses": dict(sorted(status_counts.items())),
            "documentDispositions": dict(sorted(disposition_counts.items())),
        },
        "candidates": candidate_rows,
        "releaseVerification": "passed",
        "qualificationGates": {
            "gateReceiptId": gate_receipt["gateReceiptId"],
            "evidenceSourceSetDigest": gate_receipt["evidenceSourceSetDigest"],
            "requiredGates": list(gate_receipt["requiredGates"]),
            "checks": gate_receipt["checks"],
            "gateResults": gate_receipt["gateResults"],
            "verdict": gate_receipt["verdict"],
        },
        "verdict": "passed" if passed else "failed",
    }


def write_canonical_json(path: Path, value: Any) -> None:
    """Write a new canonical qualification artifact, accepting an identical rerun."""

    path = Path(path)
    payload = canonical_json_file_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise IntegrityError(f"refusing to replace a different qualification artifact: {path}")
        return
    path.write_bytes(payload)
