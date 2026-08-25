#!/usr/bin/env python3
"""Build and run the Federal Register plus Mirrulations qualification corpus."""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path
from typing import Any

from tests.legacy_source_catalog import LocalJsonlSourceCatalog
from docspec.application.commit import ReleaseCommitService
from docspec.cli import _execute_local_run, _local_run_request, _local_storage_for_run_request
from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    require_relative_path,
    require_sha256,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileSet
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef, StoreRef
from docspec.errors import IntegrityError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.profile_registry import ProfileRegistry
from tools.fr_mirrulations_support import (
    CAMPAIGN_ID,
    MIRRULATIONS_COUNT,
    MIRRULATIONS_SCHEMA,
    CorpusInputs,
    TIERS,
    QualificationTier,
    build_candidate_census,
    build_catalogs,
    build_execution_manifest,
    build_source_items,
    reconstruct_fetcher,
    require_producer_identity,
    run_qualification_gates,
    validate_gate_receipt,
    validate_execution_manifest,
    write_canonical_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_MANIFEST = REPO_ROOT / f"fixtures/qualification/{CAMPAIGN_ID}/input-manifest.json"
DEFAULT_OUTPUT = REPO_ROOT / f"output/qualification/{CAMPAIGN_ID}"
MAXIMUM_DRAW_BYTES = 64 * 1024**2
_PROFILE_IDS = (
    "urn:docspec:profile:release-manifest:canonical-json:1",
    "urn:docspec:profile:document-catalog:local-manifest:1",
    "urn:docspec:profile:record-storage:local-jsonl:1",
    "urn:docspec:profile:blob-storage:local-content-addressed:1",
    "urn:docspec:profile:document-store-persistence:local-json:1",
    "urn:docspec:profile:result-delivery:durable-dataset:1",
)


def _canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"{label} must be a regular, non-symlink file: {path}")
    value = thaw_json(parse_canonical_json(path.read_bytes(), label=label))
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must contain an object")
    return value


def _inputs(args: argparse.Namespace, draw_path: Path) -> CorpusInputs:
    if args.federal_register_root is None:
        raise IntegrityError("--federal-register-root must name the captured Federal Register root")
    fr_root = args.federal_register_root.resolve(strict=True)
    return CorpusInputs(
        fr_root / "draw-manifest-final.json",
        fr_root / "cache-xml/receipts",
        fr_root / "cache-xml/documents",
        draw_path,
    )


def admit_producer_inputs(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    """Admit the pinned Mirrulations draw by digest and return its producer facts.

    The draw arrives already published. This reads one tracked input manifest,
    resolves the draw it pins as a contained relative path, bounds the read,
    recomputes the digest over the bytes, and refuses a mismatch before any
    parser sees them -- the shape `LocalSourceReleaseReader._reference` uses.
    """

    manifest_path = args.input_manifest.resolve(strict=True)
    manifest = _canonical_object(manifest_path, label="qualification input manifest")
    expected_fields = {"format", "formatVersion", "campaignId", "draw", "producer", "inputManifestId"}
    if set(manifest) != expected_fields:
        raise IntegrityError("qualification input manifest has an invalid closed shape")
    identity_content = dict(manifest)
    manifest_id = identity_content.pop("inputManifestId")
    if (
        manifest["format"] != "docspec-qualification-input-manifest"
        or manifest["formatVersion"] != "1.0"
        or manifest["campaignId"] != CAMPAIGN_ID
        or manifest_id != stable_urn("qualification-input-manifest", identity_content)
    ):
        raise IntegrityError("qualification input manifest identity or format is invalid")
    draw = manifest["draw"]
    if not isinstance(draw, dict) or set(draw) != {
        "path",
        "digest",
        "byteLength",
        "drawId",
        "schema",
        "documentCount",
    }:
        raise IntegrityError("pinned Mirrulations draw has an invalid closed shape")
    if draw["schema"] != MIRRULATIONS_SCHEMA or draw["documentCount"] != MIRRULATIONS_COUNT:
        raise IntegrityError("pinned Mirrulations draw does not describe this campaign's population")
    digest = require_sha256(draw["digest"], "pinned Mirrulations draw digest")
    byte_length = draw["byteLength"]
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or not 0 < byte_length <= MAXIMUM_DRAW_BYTES:
        raise IntegrityError(f"pinned Mirrulations draw must declare at most {MAXIMUM_DRAW_BYTES} bytes")
    draw_path = manifest_path.parent / require_relative_path(draw["path"], "pinned Mirrulations draw path")
    if draw_path.is_symlink() or not draw_path.is_file():
        raise IntegrityError(f"pinned Mirrulations draw must be a regular, non-symlink file: {draw_path}")
    if draw_path.stat().st_size != byte_length:
        raise IntegrityError("pinned Mirrulations draw differs in size from its input manifest")
    if sha256_digest(draw_path.read_bytes()) != digest:
        raise IntegrityError("pinned Mirrulations draw differs from its input manifest digest")
    require_producer_identity(manifest["producer"])
    return draw_path.resolve(strict=True), manifest


def verify_gates(args: argparse.Namespace) -> dict[str, Any]:
    path = args.output.resolve() / "verification/gate-receipt.json"
    if path.is_file():
        return validate_gate_receipt(path, repository=REPO_ROOT)
    receipt = run_qualification_gates(repository=REPO_ROOT)
    write_canonical_json(path, receipt)
    return validate_gate_receipt(path, repository=REPO_ROOT)


def _profiles() -> ProfileSet:
    return ProfileRegistry.from_directory(REPO_ROOT / "profiles").select(_PROFILE_IDS)


def _tier(name: str) -> QualificationTier:
    try:
        return next(tier for tier in TIERS if tier.name == name)
    except StopIteration as error:
        raise IntegrityError(f"unknown qualification tier: {name}") from error


def _tier_roots(output: Path, tier: str, source_catalog_root: Path, source_content: Path) -> dict[str, Path]:
    run_root = output / "runs" / tier
    roots = {
        "blobStorage": run_root / "blobs",
        "controlRepository": run_root / "controls",
        "documentCatalog": run_root / "catalog",
        "documentStores": run_root / "stores",
        "reconciliation": run_root / "reconciliation",
        "recordStorage": run_root / "records",
        "sourceCatalog": source_catalog_root,
        "sourceContent": source_content,
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    return roots


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Validate producer artifacts, build catalogs, plans, requests, and manifests."""

    gates = verify_gates(args)
    draw_path, input_manifest = admit_producer_inputs(args)
    inputs = _inputs(args, draw_path)
    output = args.output.resolve()
    catalog_root = output / "source-catalogs"
    references, catalog_set = build_catalogs(inputs, catalog_root=catalog_root)
    write_canonical_json(output / "catalog-set.json", catalog_set)
    federal_register, mirrulations = build_source_items(inputs)
    profiles = _profiles()
    retry = RetryPolicy(max_attempts=3, base_delay_milliseconds=250, max_delay_milliseconds=5_000)
    accepted = AcceptedFailurePolicy()
    processor = ContentStatisticsProcessor(retry_policy=retry)
    manifests: dict[str, dict[str, Any]] = {}
    runner_path = Path(__file__).resolve(strict=True)
    runner_support_path = runner_path.with_name("fr_mirrulations_support.py").resolve(strict=True)
    for tier in TIERS:
        tier_root = output / "runs" / tier.name
        tier_root.mkdir(parents=True, exist_ok=True)
        plan = ProcessingPlan.create(
            source_catalog=references[tier.name],
            base_release=None,
            profiles=profiles,
            limits=WorkLimits(
                max_entries=25,
                max_estimated_bytes=256 * 1024**2,
                max_pages_or_frames=25_000,
                max_segments=100_000,
                max_processor_cost=100_000,
                max_memory_bytes=1024**3,
                max_duration_seconds=3_600,
                max_attempts=retry.max_attempts,
            ),
            stages=StagePolicy(
                (DefaultExtractorRegistry.extractor_id,),
                DefaultSegmenterRegistry.segmenter_id,
                (processor.description.processor_id,),
            ),
            processors=ProcessorSet((processor.description,)),
            partition_count=64,
            selection={},
            retention_policy=RetentionPolicy.retain_all(),
            data_use_policy=DataUsePolicy.local_content(),
            retry_policy_digest=retry.digest,
            accepted_failure_policy_digest=accepted.digest,
        )
        plan_path = tier_root / "processing-plan.json"
        write_canonical_json(plan_path, plan.to_dict())
        roots = _tier_roots(output, tier.name, catalog_root, inputs.federal_register_content)
        request = {
            "format": "docspec-local-run-request",
            "formatVersion": "1.0",
            "plan": plan_path.resolve(strict=True).as_posix(),
            "profileDirectory": (REPO_ROOT / "profiles").resolve(strict=True).as_posix(),
            "roots": {name: path.resolve(strict=True).as_posix() for name, path in sorted(roots.items())},
            "resultSinkId": f"urn:docspec:qualification:{CAMPAIGN_ID}:sink:{tier.name}",
            "partitionPolicyId": "source-item-sha256-v1",
            "retryPolicy": retry.to_dict(),
            "acceptedFailurePolicy": accepted.to_dict(),
            "execution": {
                "maxWorkers": args.workers,
                "maxInFlight": args.workers,
                "deadlineEpochSeconds": 2_000_000_000,
                "maxScratchBytesPerWorker": 2 * 1024**3,
                "maxNetworkBytesPerTask": 256 * 1024**2,
                "requestRateLimitPerSecond": 100,
                "maxProviderConcurrency": args.workers,
                "maxTaskAttempts": 1,
                "retryInitialDelayMilliseconds": 0,
                "retryMaxDelayMilliseconds": 0,
            },
            "completedAt": "2026-08-06T12:00:00Z",
        }
        request_path = tier_root / "run-request.json"
        write_canonical_json(request_path, request)
        manifest = build_execution_manifest(
            tier=tier,
            inputs=inputs,
            federal_register=federal_register,
            mirrulations=mirrulations,
            source_catalog=references[tier.name],
            processing_plan_path=plan_path,
            processing_plan_id=plan.plan_id,
            run_request_path=request_path,
            output_roots=roots,
            mirrulations_producer=input_manifest["producer"],
            runner_path=runner_path,
            runner_support_path=runner_support_path,
            gate_receipt_path=output / "verification/gate-receipt.json",
            workers=args.workers,
            max_object_bytes=64 * 1024**2,
            retry_policy=retry.to_dict(),
        )
        manifest_path = tier_root / "execution-manifest.json"
        write_canonical_json(manifest_path, manifest)
        validate_execution_manifest(manifest_path)
        manifests[tier.name] = {"path": manifest_path.as_posix(), "manifestId": manifest["manifestId"]}
    result = {
        "format": "docspec-qualification-preparation",
        "formatVersion": "1.0",
        "campaignId": CAMPAIGN_ID,
        "inputManifest": {
            "path": args.input_manifest.resolve(strict=True).as_posix(),
            "inputManifestId": input_manifest["inputManifestId"],
        },
        "catalogs": {name: ref.to_dict() for name, ref in sorted(references.items())},
        "qualificationGates": {
            "path": (output / "verification/gate-receipt.json").as_posix(),
            "gateReceiptId": gates["gateReceiptId"],
        },
        "executionManifests": manifests,
        "verdict": "passed",
    }
    write_canonical_json(output / "preparation.json", result)
    return result


def _content_fetcher_composition(manifest_path: Path, manifest: dict[str, Any], fetcher: Any) -> dict[str, Any]:
    return {
        "implementationId": fetcher.downloader_id,
        "configurationDigest": fetcher.configuration_digest,
        "qualificationExecutionManifest": {
            "manifestId": manifest["manifestId"],
            "path": manifest_path.resolve(strict=True).as_posix(),
            "digest": sha256_digest(manifest_path.read_bytes()),
        },
    }


def _verified_census(args: argparse.Namespace, tier: str) -> dict[str, Any]:
    """Recompute all verdict-bearing census fields from the verified release."""

    output = args.output.resolve()
    tier_root = output / "runs" / tier
    path = tier_root / "candidate-census.json"
    saved = _canonical_object(path, label=f"{tier} candidate census")
    identity_content = dict(saved)
    census_id = identity_content.pop("censusId", None)
    if census_id != stable_urn("qualification-candidate-census", identity_content):
        raise IntegrityError(f"{tier} candidate census identity differs")
    manifest = validate_execution_manifest(tier_root / "execution-manifest.json")
    gate_receipt = validate_gate_receipt(Path(manifest["qualificationGates"]["path"]), repository=REPO_ROOT)
    request_path = Path(manifest["runRequest"]["path"])
    request = _local_run_request(request_path)
    _, plan, controls, stores, records, _, catalog = _local_storage_for_run_request(request_path)
    release_ref = DocumentReleaseRef.from_dict(
        _canonical_object(tier_root / "release-reference.json", label=f"{tier} release reference")
    )
    recomputed = build_candidate_census(
        tier=tier,
        source_catalog=LocalJsonlSourceCatalog(Path(request["roots"]["sourceCatalog"])),
        source_catalog_ref=SourceCatalogRef.from_dict(manifest["sourceCatalog"]),
        document_catalog=catalog,
        controls=controls,
        release_ref=release_ref,
        gate_receipt=gate_receipt,
    )
    for name, value in recomputed.items():
        if saved.get(name) != value:
            raise IntegrityError(f"{tier} candidate census field {name!r} differs from verified release state")
    expected_fields = set(recomputed) | {"measurements", "censusId"}
    if set(saved) != expected_fields:
        raise IntegrityError(f"{tier} candidate census has an invalid closed shape")
    measurements = saved["measurements"]
    measurement_fields = {
        "elapsedMilliseconds",
        "workers",
        "maximumResidentBytes",
        "storeRetries",
        "resumedStores",
        "workLimits",
        "maximumObjectBytes",
        "releaseCounts",
    }
    if not isinstance(measurements, dict) or set(measurements) != measurement_fields:
        raise IntegrityError(f"{tier} candidate census measurements have an invalid closed shape")
    for name in (
        "elapsedMilliseconds",
        "workers",
        "maximumResidentBytes",
        "storeRetries",
        "resumedStores",
        "maximumObjectBytes",
    ):
        value = measurements[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IntegrityError(f"{tier} candidate census measurement {name!r} is invalid")
    if measurements["workers"] != manifest["execution"]["workerCount"]:
        raise IntegrityError(f"{tier} candidate census worker count differs from its manifest")
    if measurements["workLimits"] != plan.limits.to_dict():
        raise IntegrityError(f"{tier} candidate census limits differ from its processing plan")
    if measurements["maximumObjectBytes"] != manifest["acquisition"]["maximumObjectBytes"]:
        raise IntegrityError(f"{tier} candidate census object limit differs from its manifest")
    verified_release = catalog.open(release_ref)
    if measurements["releaseCounts"] != verified_release.counts:
        raise IntegrityError(f"{tier} candidate census release counts differ from the verified release")
    run_ref = ArtifactRef.from_dict(
        _canonical_object(tier_root / "run-reference.json", label=f"{tier} run reference")
    )
    run = RunReceipt.from_dict(controls.load(run_ref))
    store_retries = 0
    resumed_stores = 0
    for row in records.stream(run.store_ledger):
        store = stores.load(StoreRef.from_dict(row["store"]))
        extra_attempts = max(0, len(store.attempts) - 1)
        store_retries += extra_attempts
        resumed_stores += int(extra_attempts > 0)
    if (
        measurements["storeRetries"] != store_retries
        or measurements["resumedStores"] != resumed_stores
    ):
        raise IntegrityError(f"{tier} candidate census retry measurements differ from verified run state")
    return saved


def _require_predecessor(args: argparse.Namespace, tier: str) -> None:
    predecessor = {"intermediate": "smoke", "full": "intermediate"}.get(tier)
    if predecessor is None:
        return
    path = args.output.resolve() / f"runs/{predecessor}/candidate-census.json"
    if not path.is_file():
        raise IntegrityError(f"{predecessor} tier must close and pass before {tier} starts")
    value = _verified_census(args, predecessor)
    if value.get("verdict") != "passed":
        raise IntegrityError(f"{predecessor} tier did not pass")


def run_tier(args: argparse.Namespace, tier_name: str) -> dict[str, Any]:
    """Run one prepared tier through execution, commit, verification, and census."""

    tier = _tier(tier_name)
    output = args.output.resolve()
    _require_predecessor(args, tier.name)
    tier_root = output / "runs" / tier.name
    if (tier_root / "candidate-census.json").is_file():
        return _verified_census(args, tier.name)
    manifest_path = tier_root / "execution-manifest.json"
    manifest = validate_execution_manifest(manifest_path)
    gate_receipt = validate_gate_receipt(Path(manifest["qualificationGates"]["path"]), repository=REPO_ROOT)
    fetcher = reconstruct_fetcher(manifest)
    composition = _content_fetcher_composition(manifest_path, manifest, fetcher)
    request_path = Path(manifest["runRequest"]["path"])
    request = _local_run_request(request_path)
    started = time.monotonic()
    run_ref = _execute_local_run(
        request,
        resume=None,
        content_fetcher=fetcher,
        content_fetcher_composition=composition,
    )
    write_canonical_json(tier_root / "run-reference.json", run_ref.to_dict())
    _, plan, controls, stores, records, _, catalog = _local_storage_for_run_request(request_path)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    release_ref = ReleaseCommitService(
        plan_ref=plan_ref,
        controls=controls,
        records=records,
        document_catalog=catalog,
    ).commit_release(None, run_ref)
    write_canonical_json(tier_root / "release-reference.json", release_ref.to_dict())
    # A fresh catalog open re-verifies the release root, every linked artifact,
    # every layer, every store receipt, and every retained blob.
    verified_release = catalog.open(release_ref)
    source_catalog_ref = SourceCatalogRef.from_dict(manifest["sourceCatalog"])
    census = build_candidate_census(
        tier=tier.name,
        source_catalog=LocalJsonlSourceCatalog(Path(request["roots"]["sourceCatalog"])),
        source_catalog_ref=source_catalog_ref,
        document_catalog=catalog,
        controls=controls,
        release_ref=release_ref,
        gate_receipt=gate_receipt,
    )
    run = RunReceipt.from_dict(controls.load(run_ref))
    store_retries = 0
    resumed_stores = 0
    for row in records.stream(run.store_ledger):
        store = stores.load(StoreRef.from_dict(row["store"]))
        extra_attempts = max(0, len(store.attempts) - 1)
        store_retries += extra_attempts
        resumed_stores += int(extra_attempts > 0)
    elapsed = time.monotonic() - started
    census["measurements"] = {
        "elapsedMilliseconds": int(elapsed * 1_000),
        "workers": args.workers,
        "maximumResidentBytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "storeRetries": store_retries,
        "resumedStores": resumed_stores,
        "workLimits": plan.limits.to_dict(),
        "maximumObjectBytes": manifest["acquisition"]["maximumObjectBytes"],
        "releaseCounts": verified_release.counts,
    }
    census = {**census, "censusId": stable_urn("qualification-candidate-census", census)}
    write_canonical_json(tier_root / "candidate-census.json", census)
    if census["verdict"] != "passed":
        raise IntegrityError(f"{tier.name} qualification closed with a failed verdict")
    return census


def _markdown_report(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Federal Register and Mirrulations qualification report",
        "",
        f"Campaign: `{CAMPAIGN_ID}`",
        "",
        "| Tier | Documents | Candidates | Bytes | Processed | Failed | Not attempted | Elapsed | Retries | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        counts = result["counts"]
        statuses = counts["candidateStatuses"]
        failed = statuses["acquisition-failed"] + statuses["processing-failed"]
        elapsed = result["measurements"]["elapsedMilliseconds"]
        lines.append(
            f"| {result['tier']} | {counts['documents']:,} | {counts['requiredCandidates']:,} | "
            f"{counts['requiredBytes']:,} | {statuses['processed']:,} | {failed:,} | "
            f"{statuses['not-attempted']:,} | {elapsed / 1000:.1f}s | "
            f"{result['measurements']['storeRetries']:,} | {result['verdict']} |"
        )
    final = results[-1]
    final_counts = final["counts"]
    final_measurements = final["measurements"]
    gates = final["qualificationGates"]
    lines.extend(
        [
            "",
            "## Full-tier composition",
            "",
            f"- Sources: Federal Register {final_counts['sources'].get('federal-register', 0):,}; "
            f"Mirrulations {final_counts['sources'].get('mirrulations', 0):,}.",
            "- Media types: "
            + "; ".join(f"{name} {count:,}" for name, count in final_counts["mediaTypes"].items())
            + ".",
            f"- Workers: {final_measurements['workers']:,}; resumed stores: "
            f"{final_measurements['resumedStores']:,}; maximum resident memory: "
            f"{final_measurements['maximumResidentBytes']:,} bytes.",
            f"- Per-object limit: {final_measurements['maximumObjectBytes']:,} bytes; "
            f"work limits: `{final_measurements['workLimits']}`.",
            "",
            "## Validation evidence",
            "",
            f"The sealed gate receipt `{gates['gateReceiptId']}` passed: "
            + ", ".join(gates["requiredGates"])
            + ".",
            f"Lint: {gates['checks']['lint']['verdict']}. Full repository tests: "
            f"{gates['checks']['tests']['passed']:,} passed.",
            "Every completed tier passed independent release verification, and each candidate census "
            "was recomputed from its verified release before the next tier started.",
            "",
            f"The final qualification verdict is **{final['verdict']}**.",
            "This campaign is real-world qualification evidence; it does not change DocSpec's formal conformance status.",
            "",
        ]
    )
    return "\n".join(lines)


def run_all(args: argparse.Namespace) -> dict[str, Any]:
    prepare(args)
    results = [run_tier(args, tier.name) for tier in TIERS]
    verdict = "passed" if all(result["verdict"] == "passed" for result in results) else "failed"
    report = {
        "format": "docspec-qualification-report",
        "formatVersion": "1.0",
        "campaignId": CAMPAIGN_ID,
        "tiers": [
            {
                "tier": result["tier"],
                "counts": result["counts"],
                "release": result["release"],
                "logicalStateDigest": result["logicalStateDigest"],
                "measurements": result["measurements"],
                "releaseVerification": result["releaseVerification"],
                "qualificationGates": result["qualificationGates"],
                "verdict": result["verdict"],
            }
            for result in results
        ],
        "formalConformanceStatusChanged": False,
        "verdict": verdict,
    }
    write_canonical_json(args.output.resolve() / "qualification-report.json", report)
    markdown = args.output.resolve() / "qualification-report.md"
    rendered = _markdown_report(results).encode("utf-8")
    if markdown.exists() and markdown.read_bytes() != rendered:
        raise IntegrityError(f"refusing to replace a different qualification artifact: {markdown}")
    if not markdown.exists():
        markdown.write_bytes(rendered)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--input-manifest", type=Path, default=DEFAULT_INPUT_MANIFEST)
    # The captured Federal Register root holds hundreds of megabytes of cached
    # source bytes, so it is named by the operator rather than pinned in tree.
    parser.add_argument("--federal-register-root", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=8)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    run = commands.add_parser("run-tier")
    run.add_argument("tier", choices=[tier.name for tier in TIERS])
    commands.add_parser("run-all")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")
    if args.command == "prepare":
        result = prepare(args)
    elif args.command == "run-tier":
        result = run_tier(args, args.tier)
    else:
        result = run_all(args)
    emitted = result
    if result.get("format") == "docspec-qualification-candidate-census":
        emitted = {name: value for name, value in result.items() if name != "candidates"}
    sys.stdout.buffer.write(canonical_json_file_bytes(emitted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
