"""Dependency-light CLI for DocSpec-owned immutable source catalogs."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from docspec.adapters.atomic_directory import (
    publish_directory_no_replace,
    sync_directory,
    write_file_no_replace,
)
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_artifact import (
    SourceCatalogArtifactReader,
    SourceCatalogBuildRequest,
    SourceCatalogBuilder,
    source_catalog_producer,
)
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    parse_closed_json,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import SourceCatalogRef
from docspec.domain.security import redact, redact_text, require_secret_free
from docspec.errors import DocSpecError, IntegrityError

_MAX_JSON_BYTES = 16 * 1024 * 1024


class SourceCatalogCliError(DocSpecError):
    """A source-catalog operator action failed preflight or verification."""


def _read_bytes(path: Path, *, label: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SourceCatalogCliError(f"{label} must be a regular, non-symlink file: {path}")
    if path.stat().st_size > _MAX_JSON_BYTES:
        raise SourceCatalogCliError(f"{label} exceeds the {_MAX_JSON_BYTES}-byte limit")
    return path.read_bytes()


def _read_object(path: Path, *, label: str, canonical: bool) -> dict[str, Any]:
    payload = _read_bytes(path, label=label)
    parser = parse_canonical_json if canonical else parse_closed_json
    value = thaw_json(parser(payload, label=label))
    if not isinstance(value, dict):
        raise SourceCatalogCliError(f"{label} must be a JSON object")
    return value


def _existing_root(path: Path, *, label: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise SourceCatalogCliError(f"{label} must be an existing, non-symlink directory: {path}")
    return path.resolve(strict=True)


def _emit(value: object, *, error: bool = False) -> None:
    if error:
        value = redact(value)
    else:
        require_secret_free(value, label="CLI output")
    stream = sys.stderr.buffer if error else sys.stdout.buffer
    stream.write(canonical_json_file_bytes(value))
    stream.flush()


def _write_new(path: Path, payload: bytes, *, label: str) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise SourceCatalogCliError(f"refusing to replace existing {label}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_file_no_replace(path, payload)
    except IntegrityError as error:
        raise SourceCatalogCliError(f"refusing to replace existing {label}: {path}") from error


def _require_new_outputs(destination: Path, receipt: Path) -> None:
    if destination.resolve(strict=False) == receipt.resolve(strict=False):
        raise SourceCatalogCliError("artifact destination and receipt path must differ")
    for path, label in ((destination, "artifact"), (receipt, "operation receipt")):
        if path.exists() or path.is_symlink():
            raise SourceCatalogCliError(f"refusing to replace existing {label}: {path}")


def _write_failure_receipt(args: argparse.Namespace, error: Exception) -> None:
    receipt_value = getattr(args, "receipt", None)
    if receipt_value is None:
        return
    receipt_path = Path(receipt_value)
    if receipt_path.exists() or receipt_path.is_symlink():
        return
    content = {
        "operation": getattr(args, "operation", "source-catalog"),
        "requestDigest": None,
        "errorType": type(error).__name__,
        "diagnosticCode": f"DOCSPEC-CLI-{type(error).__name__.upper()}",
        "verdict": "failed",
    }
    receipt = {
        "format": "docspec-operation-failure-receipt",
        "formatVersion": "1.0",
        "receiptId": stable_urn("operation-failure-receipt", content),
        **content,
    }
    try:
        _write_new(
            receipt_path,
            canonical_json_file_bytes(receipt),
            label="operation failure receipt",
        )
    except (DocSpecError, OSError):
        return


def _producer(args: argparse.Namespace):
    return source_catalog_producer(
        implementation_id=args.implementation_id,
        verifier_id="urn:docspec:verifier:source-catalog",
        verifier_version="1.0.0",
        verifier_implementation_id=args.verifier_implementation_id,
    )


def _verify(args: argparse.Namespace) -> int:
    reference = SourceCatalogRef.from_dict(
        _read_object(args.reference, label="source catalog reference", canonical=False)
    )
    summary = SourceCatalogArtifactReader(
        LocalSourceCatalogStore(
            _existing_root(args.root, label="source catalog root"),
            create=False,
        ),
        producer=_producer(args),
    ).verify_snapshot(reference)
    _emit(
        {
            "format": "docspec-source-catalog-verification",
            "formatVersion": "1.0",
            "logicalId": summary.logical_id,
            "artifactDigest": summary.artifact_digest,
            "catalogId": summary.catalog_id,
            "catalogStateDigest": summary.catalog_state_digest,
            "requestedUniverseSetDigest": summary.requested_universe_set_digest,
            "selectedSourceSetDigest": summary.selected_source_set_digest,
            "itemCount": summary.item_count,
            "itemMemberPath": summary.item_member_path,
            "partitions": list(summary.partitions),
            "dispositionCounts": dict(summary.disposition_counts),
            "verdict": "pass",
        }
    )
    return 0


def _build(args: argparse.Namespace) -> int:
    lengths = {
        len(args.source_native),
        len(args.source_native_artifact_digest),
        len(args.source_native_profile),
    }
    if len(lengths) != 1:
        raise SourceCatalogCliError(
            "each --source-native requires one --source-native-artifact-digest and --source-native-profile"
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

    # Import the producer adapter only after the operator selects it. Help and
    # verification do not require the producer package.
    from docspec.adapters.spicyregs_source_native import (
        SpicyRegsSourceNativeAdapter,
        spicyregs_source_profile,
    )

    sources = tuple(
        SpicyRegsSourceNativeAdapter.from_local(
            _existing_root(locator, label="source-native artifact"),
            artifact_digest=digest,
            profile=spicyregs_source_profile(profile_name),
            accepted_verifier_implementation_ids=accepted_verifiers,
        )
        for locator, digest, profile_name in zip(
            args.source_native,
            args.source_native_artifact_digest,
            args.source_native_profile,
            strict=True,
        )
    )
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
    receipt_path = Path(args.receipt)
    _require_new_outputs(destination, receipt_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".staging",
            dir=destination.parent,
        )
    )
    try:
        result = SourceCatalogBuilder(
            store=LocalSourceCatalogStore(staging_root),
            policy=policy,
            request=SourceCatalogBuildRequest(catalog_id, producer),
            workspace_factory=lambda: SqliteCatalogPolicyWorkspace(
                directory=destination.parent
            ),
        ).build(sources)
        (staging_root / ".staging").rmdir()
        content = {
            "operation": "source-catalog.build",
            "sourceNativeInputs": [
                {
                    "locator": Path(locator).resolve(strict=True).as_posix(),
                    "logicalId": description.logical_id,
                    "artifactDigest": description.artifact_digest,
                }
                for locator, description in zip(args.source_native, descriptions, strict=True)
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
            "verdict": "pass",
        }
        receipt = {
            "format": "docspec-source-catalog-build-command-receipt",
            "formatVersion": "1.0",
            "receiptId": stable_urn("source-catalog-build-command-receipt", content),
            **content,
        }
        _write_new(
            receipt_path,
            canonical_json_file_bytes(receipt),
            label="source-catalog build receipt",
        )
        try:
            publish_directory_no_replace(staging_root, destination)
        except (DocSpecError, OSError):
            receipt_path.unlink(missing_ok=True)
            sync_directory(receipt_path.parent)
            raise
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)
    _emit(receipt)
    return 0


def _add_subcommands(source_catalog: argparse.ArgumentParser) -> None:
    source_commands = source_catalog.add_subparsers(dest="source_catalog_command", required=True)
    source_build = source_commands.add_parser("build", help="Build one complete immutable source-catalog snapshot")
    source_build.add_argument("--source-native", action="append", type=Path, required=True)
    source_build.add_argument("--source-native-artifact-digest", action="append", required=True)
    source_build.add_argument(
        "--source-native-profile",
        action="append",
        required=True,
        choices=(
            "federal-register",
            "regulations-gov-documents",
            "regulations-gov-dockets",
        ),
    )
    source_build.add_argument("--accepted-source-verifier-implementation-id", action="append", required=True)
    source_build.add_argument("--catalog-policy", type=Path, required=True)
    source_build.add_argument("--implementation-id", required=True)
    source_build.add_argument("--verifier-implementation-id", required=True)
    source_build.add_argument("--destination", type=Path, required=True)
    source_build.add_argument("--receipt", type=Path, required=True)
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
        _write_failure_receipt(args, error)
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
