"""Dependency-light CLI for DocSpec-owned immutable source catalogs."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.catalog_artifact.builder import (
    SourceCatalogBuildRequest,
    SourceCatalogBuilder,
    _snapshot_sources,
)
from docspec.adapters.catalog_artifact.rules import source_catalog_producer
from docspec.adapters.source_catalog_store import (
    LocalSourceCatalogPublication,
    LocalSourceCatalogStore,
)
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from docspec.domain.identity import stable_urn
from docspec.domain.references import SourceCatalogRef
from docspec.domain.source_outcomes import (
    DEFAULT_ACCEPTED_RECORD_OUTCOMES, RECORD_OUTCOMES, accepted_record_outcomes,
)
from docspec.ports.source_catalog import SourceNativeDescription
from docspec.domain.security import redact_text
from docspec.errors import DocSpecError

from docspec.cli.catalog_policy import add_policy_command
from docspec.cli_io import (
    SourceCatalogCliError,
    emit as _emit,
    existing_root,
    read_object,
)


def _read_object(path: Path, *, label: str, canonical: bool) -> dict[str, Any]:
    return read_object(path, label=label, canonical=canonical, error_type=SourceCatalogCliError)


def _existing_root(path: Path, *, label: str) -> Path:
    return existing_root(path, label=label, error_type=SourceCatalogCliError)


_SOURCE_NATIVE_PROFILES = (
    "federal-register",
    "regulations-gov-documents",
    "regulations-gov-dockets",
    "regulations-gov-comments",
)


def _paths_overlap(first: Path, second: Path) -> bool:
    resolved_first = Path(first).resolve(strict=False)
    resolved_second = Path(second).resolve(strict=False)
    return (
        resolved_first == resolved_second
        or resolved_first in resolved_second.parents
        or resolved_second in resolved_first.parents
    )


def _blob_store_evidence(
    args: argparse.Namespace,
    measurements: Mapping[str, int],
) -> dict[str, object] | None:
    value = getattr(args, "blob_store", None)
    if value is None:
        return None
    return {
        "path": Path(value).resolve(strict=False).as_posix(),
        "retention": "verified-content-addressed-blobs-retained-for-reuse",
        "accountingStatus": "complete",
        "payloadBytesWritten": measurements["payloadBytesWritten"],
        "payloadBytesReused": measurements["payloadBytesReused"],
    }


def _require_new_outputs(
    destination: Path,
    blob_store: Path | None,
    source_native_roots: tuple[Path, ...],
) -> None:
    if blob_store is not None and _paths_overlap(destination, blob_store):
        raise SourceCatalogCliError("artifact and blob store paths must not contain one another")
    if any(_paths_overlap(destination, root) for root in source_native_roots):
        raise SourceCatalogCliError(
            "artifact and source-native input paths must not contain one another"
        )
    if destination.exists() or destination.is_symlink():
        raise SourceCatalogCliError(f"refusing to replace existing artifact: {destination}")


def _producer(args: argparse.Namespace):
    return source_catalog_producer(
        implementation_id=args.implementation_id,
        verifier_id="urn:docspec:verifier:source-catalog",
        verifier_version="1.0.0",
        verifier_implementation_id=args.verifier_implementation_id,
    )


def _verify(args: argparse.Namespace) -> int:
    root = _existing_root(args.root, label="source catalog root")
    reference = SourceCatalogRef.from_dict(
        _read_object(args.reference, label="source catalog reference", canonical=False)
    )
    summary = SourceCatalogArtifactReader(
        LocalSourceCatalogStore(root, create=False), producer=_producer(args),
    ).verify_snapshot(reference)
    _emit(
        {
            "format": "docspec-source-catalog-verification",
            "formatVersion": "2.0",
            "logicalId": summary.logical_id,
            "artifactDigest": summary.artifact_digest,
            "catalogId": summary.catalog_id,
            "catalogStateDigest": summary.catalog_state_digest,
            "requestedUniverseSetDigest": summary.requested_universe_set_digest,
            "selectedSourceSetDigest": summary.selected_source_set_digest,
            "itemCount": summary.item_count,
            "partitions": list(summary.partitions),
            "dispositionCounts": dict(summary.disposition_counts),
            "reasonCounts": [dict(value) for value in summary.reason_counts],
            "selectionPolicy": dict(summary.selection_policy),
            "partitionPolicy": dict(summary.partition_policy),
            "joinCoverage": [dict(value) for value in summary.join_coverage],
            "diagnosticDigests": dict(summary.diagnostic_digests),
            "sourceNativeInputs": [SourceNativeDescription.from_dict(value).to_dict() for value in summary.source_native_inputs],
            "acceptedRecordOutcomes": sorted(summary.accepted_record_outcomes),
            "byteMeasurements": dict(summary.byte_measurements),
            "verdict": "pass",
        }
    )
    return 0


def _build(args: argparse.Namespace) -> int:
    lengths = {
        len(args.source_native),
        len(args.source_native_artifact_digest),
        len(args.source_native_blob_store),
        len(args.source_native_profile),
    }
    if len(lengths) != 1:
        raise SourceCatalogCliError(
            "each --source-native requires one --source-native-artifact-digest, "
            "--source-native-blob-store, and --source-native-profile"
        )
    policy_member = _read_object(args.catalog_policy, label="catalog policy", canonical=True)
    policy_id = policy_member.get("policyId")
    if policy_id == FederalRegisterCatalogPolicy.policy_id:
        policy = FederalRegisterCatalogPolicy.from_member(policy_member)
    elif policy_id == RegulationsGovCatalogPolicy.policy_id:
        policy = RegulationsGovCatalogPolicy.from_member(policy_member)
    else:
        raise SourceCatalogCliError("catalog policy is not implemented by this DocSpec version")
    accepted_verifiers = frozenset(args.accepted_source_verifier_implementation_id)
    accepted_outcomes = accepted_record_outcomes(
        DEFAULT_ACCEPTED_RECORD_OUTCOMES if args.accepted_record_outcome is None else args.accepted_record_outcome
    )

    # Import the producer adapter only after the operator selects it. Help and
    # verification do not require the producer package.
    from docspec.adapters.spicy_docs_source_native import (
        SpicyDocsSourceNativeAdapter,
        spicy_docs_source_profile,
    )

    source_inputs = tuple(
        (
            _existing_root(locator, label="source-native artifact"),
            digest,
            _existing_root(blob_root, label="source-native blob store"),
            profile_name,
        )
        for locator, digest, blob_root, profile_name in zip(
            args.source_native,
            args.source_native_artifact_digest,
            args.source_native_blob_store,
            args.source_native_profile,
            strict=True,
        )
    )
    sources = tuple(
        SpicyDocsSourceNativeAdapter.from_local(
            locator,
            blob_root=blob_root,
            artifact_digest=digest,
            profile=spicy_docs_source_profile(profile_name),
            accepted_verifier_implementation_ids=accepted_verifiers,
        )
        for locator, digest, blob_root, profile_name in source_inputs
    )
    sources = _snapshot_sources(sources, accepted_outcomes)
    descriptions = tuple(source.describe() for source in sources)
    catalog_id = stable_urn(
        "source-catalog-series",
        {
            "policyId": policy.policy_id,
            "sourceSystemIds": sorted({value.source_system_id for value in descriptions}),
        },
    )
    producer = _producer(args)
    destination = Path(args.destination)
    blob_store = Path(args.blob_store) if args.blob_store is not None else None
    _require_new_outputs(
        destination,
        blob_store,
        tuple(source_input[0] for source_input in source_inputs),
    )
    # Diagnostics use stderr; the build report uses stdout.
    _emit(
        {
            "format": "docspec-source-catalog-build-diagnostic",
            "formatVersion": "1.0",
            "sourceItemValidator": "jsonschema-rs",
        },
        error=True,
    )
    with LocalSourceCatalogPublication(destination) as publication:
        destination = publication.destination
        catalog_store = publication.store(shared_blob_root=blob_store)
        if blob_store is not None:
            blob_store = blob_store.resolve(strict=True)
            args.blob_store = blob_store
        result = SourceCatalogBuilder(
            store=catalog_store,
            policy=policy,
            request=SourceCatalogBuildRequest(catalog_id, producer, accepted_record_outcomes=accepted_outcomes),
            workspace_factory=lambda: (
                SqliteCatalogPolicyWorkspace(path=args.resume_workspace)
                if args.resume_workspace is not None
                else SqliteCatalogPolicyWorkspace(directory=publication.root)
            ),
        ).build(sources)
        # Which engine derived the digests. The parallel engine falls back to
        # serial silently when its workers cannot start, and a run that does
        # not know which path it took cannot scope its timing or memory.
        _emit(
            {
                "format": "docspec-source-catalog-build-diagnostic",
                "formatVersion": "1.0",
                "derivation": {stage: dict(engine) for stage, engine in result.derivation.items()},
            },
            error=True,
        )
        if args.resume_workspace is not None:
            # Published, so the workspace is now tens of gigabytes of nothing.
            for suffix in ("", "-journal"):
                Path(f"{args.resume_workspace}{suffix}").unlink(missing_ok=True)
        publication.remove_empty_directory(".staging")
        content = {
            "operation": "source-catalog.build",
            "acceptedSourceVerifierImplementationIds": sorted(accepted_verifiers),
            "acceptedRecordOutcomes": sorted(accepted_outcomes),
            "sourceNativeInputs": [
                {
                    "locator": Path(locator).resolve(strict=True).as_posix(),
                    "blobStore": Path(blob_root).resolve(strict=True).as_posix(),
                    "profile": profile_name,
                    **description.to_dict(),
                }
                for (locator, _, blob_root, profile_name), description in zip(
                    source_inputs,
                    descriptions,
                    strict=True,
                )
            ],
            "catalogPolicy": {
                "policyId": policy.policy_id,
                "policyVersion": policy.policy_version,
                "policyDigest": policy.policy_digest,
            },
            "producer": producer.as_dict(),
            "destination": destination.resolve(strict=False).as_posix(),
            "catalog": result.reference.to_dict(),
            "catalogStateDigest": result.summary.catalog_state_digest,
            "requestedUniverseSetDigest": result.summary.requested_universe_set_digest,
            "selectedSourceSetDigest": result.summary.selected_source_set_digest,
            "itemCount": result.summary.item_count,
            "dispositionCounts": dict(result.summary.disposition_counts),
            "reasonCounts": [dict(value) for value in result.summary.reason_counts],
            "partitionPolicy": dict(result.summary.partition_policy),
            "joinCoverage": [dict(value) for value in result.summary.join_coverage],
            "diagnosticDigests": dict(result.summary.diagnostic_digests),
            "derivation": {stage: dict(engine) for stage, engine in result.derivation.items()},
            "byteMeasurements": dict(result.byte_measurements),
            "blobStore": _blob_store_evidence(args, result.byte_measurements),
            "verdict": "pass",
        }
        report = {"format": "docspec-source-catalog-build-report", "formatVersion": "1.0", **content}
        publication.publish()
    _emit(report)
    return 0


def _add_subcommands(source_catalog: argparse.ArgumentParser) -> None:
    source_commands = source_catalog.add_subparsers(dest="source_catalog_command", required=True)
    add_policy_command(source_commands)
    source_build = source_commands.add_parser("build", help="Build one complete immutable source-catalog snapshot")
    source_build.add_argument("--source-native", action="append", type=Path, required=True)
    source_build.add_argument("--source-native-artifact-digest", action="append", required=True)
    source_build.add_argument(
        "--source-native-blob-store",
        action="append",
        type=Path,
        required=True,
        help="Read-only content-addressed blob store paired with one source-native input",
    )
    source_build.add_argument(
        "--source-native-profile",
        action="append",
        required=True,
        choices=_SOURCE_NATIVE_PROFILES,
    )
    source_build.add_argument("--accepted-source-verifier-implementation-id", action="append", required=True)
    source_build.add_argument(
        "--accepted-record-outcome", action="append", choices=sorted(RECORD_OUTCOMES),
        help="Repeat for each accepted provider outcome; defaults to empty and no-record-rejections",
    )
    source_build.add_argument("--catalog-policy", type=Path, required=True)
    source_build.add_argument("--implementation-id", required=True)
    source_build.add_argument("--verifier-implementation-id", required=True)
    source_build.add_argument("--destination", type=Path, required=True)
    source_build.add_argument(
        "--resume-workspace",
        type=Path,
        default=None,
        help=(
            "Keep the build workspace at this path and commit it as the build"
            " progresses. A build that dies leaves it behind; running the same"
            " command again resumes from the last commit and publishes the"
            " identical artifact. Removed after a successful publish."
        ),
    )
    source_build.add_argument(
        "--blob-store",
        type=Path,
        help=(
            "Explicit persistent content-addressed blob store used for verified reuse; "
            "must share the destination filesystem"
        ),
    )
    source_build.set_defaults(func=_build, operation="source-catalog.build")

    source_verify = source_commands.add_parser("verify", help="Verify a complete local source-catalog distribution")
    source_verify.add_argument("--root", type=Path, required=True)
    source_verify.add_argument("--reference", type=Path, required=True, help="JSON SourceCatalogRef")
    source_verify.add_argument("--implementation-id", required=True)
    source_verify.add_argument("--verifier-implementation-id", required=True)
    source_verify.set_defaults(func=_verify)


def add_source_catalog_command(commands: argparse._SubParsersAction) -> None:
    source_catalog = commands.add_parser(
        "source-catalog",
        help="Build and verify immutable source-catalog inputs",
    )
    _add_subcommands(source_catalog)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docspec source-catalog",
        description="Build and verify DocSpec-owned immutable source catalogs.",
    )
    _add_subcommands(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (DocSpecError, OSError, TypeError, ValueError) as error:
        _emit(
            {
                "format": "docspec-cli-error",
                "formatVersion": "1.0",
                "errorType": type(error).__name__,
                "message": redact_text(str(error)),
                "verdict": "fail",
            },
            error=True,
        )
        return 2


__all__ = ["add_source_catalog_command", "build_parser", "main"]
