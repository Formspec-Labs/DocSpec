"""One operator entry point for the standalone DocSpec lifecycle."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from docspec.cli.blobs import _cmd_blob_store_gc, _cmd_blob_store_verify
from docspec.cli.catalog import (
    _cmd_document_catalog_audit, _cmd_document_catalog_compare, _cmd_document_catalog_open, _cmd_document_catalog_select,
)
from docspec.cli.common import _write_failure_receipt
from docspec.cli.evidence import _cmd_run_status, _cmd_sink_verify
from docspec.cli.inspection import add_inspection_command
from docspec.cli.plans import _cmd_document_store_create, _cmd_document_store_verify, _cmd_plan_create
from docspec.cli.profiles import (
    _cmd_profile_list,
    _cmd_profile_verify,
)
from docspec.cli.releases import (
    _cmd_document_release_save,
    _cmd_document_release_compact,
    _cmd_document_release_diff,
    _cmd_document_release_verify,
)
from docspec.cli.runs import (
    _cmd_local_run,
    _cmd_local_run_prepare,
    _cmd_local_run_reconcile,
    _cmd_local_task_execute,
    _cmd_run_active,
)
from docspec.cli_io import (
    CliError,
)
from docspec.cli_io import (
    emit as _emit,
)
from docspec.domain.security import redact_text
from docspec.errors import DocSpecError
from docspec.cli.source_catalog import add_source_catalog_command


def _add_local_catalog_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog-root", type=Path, required=True, help="Existing local document-catalog root")
    parser.add_argument("--blob-root", type=Path, required=True, help="Existing local immutable-blob root")
    parser.add_argument("--record-root", type=Path, required=True, help="Existing local record-storage root")
    parser.add_argument("--store-root", type=Path, required=True, help="Existing local document-store root")
    parser.add_argument("--control-root", type=Path, required=True, help="Existing local control-artifact root")
    parser.add_argument("--implementation-id", required=True)
    parser.add_argument("--verifier-implementation-id", required=True)


def _add_mutating_paths(
    parser: argparse.ArgumentParser,
    *,
    operation: str,
    func: Any,
) -> None:
    parser.add_argument("--request", type=Path, required=True, help="Closed JSON operation request")
    parser.add_argument("--destination", type=Path, required=True, help="New destination; replacement is refused")
    parser.add_argument("--receipt", type=Path, required=True, help="New machine receipt; replacement is refused")
    parser.set_defaults(func=func, operation=operation)


def _subcommands(parser: argparse.ArgumentParser, *, dest: str) -> argparse._SubParsersAction:
    return parser.add_subparsers(dest=dest, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docspec", description=__doc__)
    commands = _subcommands(parser, dest="command")

    add_source_catalog_command(commands)
    add_inspection_command(commands)

    profile = commands.add_parser("profile", help="Inspect storage and delivery profile descriptions")
    profile_commands = _subcommands(profile, dest="profile_command")
    profile_list = profile_commands.add_parser("list", help="List installed profiles or an explicit profile directory")
    profile_list.add_argument("--directory", type=Path)
    profile_list.set_defaults(func=_cmd_profile_list)
    profile_verify = profile_commands.add_parser("verify", help="Verify one closed profile description")
    profile_verify.add_argument("profile", type=Path)
    profile_verify.set_defaults(func=_cmd_profile_verify)

    document_catalog = commands.add_parser("document-catalog", help="Open, audit, compare, and select retained results")
    catalog_commands = _subcommands(document_catalog, dest="document_catalog_command")
    _add_mutating_paths(
        catalog_commands.add_parser("select", help="Select a retained result if current still matches the request"),
        operation="document-catalog.select",
        func=_cmd_document_catalog_select,
    )
    for name, help_text, handler in (
        ("open", "Open pinned metadata without scanning retained data", _cmd_document_catalog_open),
        ("audit", "Verify all retained records, bytes and execution evidence", _cmd_document_catalog_audit),
    ):
        command = catalog_commands.add_parser(name, help=help_text)
        _add_local_catalog_arguments(command)
        command.add_argument("--reference", type=Path, required=True)
        command.set_defaults(func=handler)
    catalog_compare = catalog_commands.add_parser("compare", help="Compare one logical layer across two releases")
    _add_local_catalog_arguments(catalog_compare)
    catalog_compare.add_argument("--older-reference", type=Path, required=True)
    catalog_compare.add_argument("--newer-reference", type=Path, required=True)
    catalog_compare.add_argument("--layer-kind", required=True)
    catalog_compare.add_argument("--sample-limit", type=int, default=20)
    catalog_compare.set_defaults(func=_cmd_document_catalog_compare)

    plan = commands.add_parser("plan", help="Create immutable processing plans")
    plan_commands = _subcommands(plan, dest="plan_command")
    plan_create = plan_commands.add_parser("create", help="Create a ProcessingPlan from a closed JSON request")
    plan_create.add_argument("--request", type=Path, required=True)
    plan_create.add_argument("--destination", type=Path, required=True)
    plan_create.add_argument("--receipt", type=Path, required=True)
    plan_create.set_defaults(func=_cmd_plan_create, operation="plan.create")

    document_store = commands.add_parser("document-store", help="Create and verify bounded work jobs")
    store_commands = _subcommands(document_store, dest="document_store_command")
    store_create = store_commands.add_parser("create", help="Create one planned DocumentStore")
    store_create.add_argument("--request", type=Path, required=True)
    store_create.add_argument("--destination", type=Path, required=True)
    store_create.add_argument("--receipt", type=Path, required=True)
    store_create.set_defaults(func=_cmd_document_store_create, operation="document-store.create")
    store_verify = store_commands.add_parser("verify", help="Verify one canonical DocumentStore revision")
    store_verify.add_argument("store", type=Path)
    store_verify.add_argument("--root", type=Path, help="Repository root for a saved store with entry members")
    store_verify.add_argument("--max-revision-bytes", type=int, default=64 * 1024**2)
    store_verify.add_argument("--max-inline-bytes", type=int, default=1024**2)
    store_verify.set_defaults(func=_cmd_document_store_verify)

    run = commands.add_parser("run", help="Start, resume, and inspect scheduler-neutral runs")
    run_commands = _subcommands(run, dest="run_command")
    _add_mutating_paths(
        run_commands.add_parser("prepare", help="Save bounded jobs and seal an execution handoff"),
        operation="run.prepare",
        func=_cmd_local_run_prepare,
    )
    _add_mutating_paths(
        run_commands.add_parser("start", help="Execute a new run through the portable local profile"),
        operation="run.start",
        func=_cmd_local_run,
    )
    _add_mutating_paths(
        run_commands.add_parser("resume", help="Resume saved local jobs and finish their run"),
        operation="run.resume",
        func=_cmd_local_run,
    )
    _add_mutating_paths(
        run_commands.add_parser("reconcile", help="Verify a saved terminal task-result stream"),
        operation="run.reconcile",
        func=_cmd_local_run_reconcile,
    )
    run_status = run_commands.add_parser("status", help="Verify and summarize a sealed RunReceipt")
    run_status.add_argument("--receipt", type=Path, required=True)
    run_status.add_argument("--control-root", type=Path, help="Resolve an ArtifactRef from this control repository")
    run_status.set_defaults(func=_cmd_run_status)
    run_active = run_commands.add_parser(
        "active",
        help="Report bounded, read-only progress for a run that has not finished",
    )
    run_active.add_argument(
        "--request",
        type=Path,
        required=True,
        help="The same closed local run request 'run prepare/start/resume' use",
    )
    run_active.add_argument(
        "--stalled-after-seconds",
        type=int,
        default=900,
        help="How long a running store may go unobserved before it is reported as stalled",
    )
    run_active.add_argument(
        "--stalled-sample-limit",
        type=int,
        default=20,
        help="Maximum stalled store ids to list; stalledStoreCount is always exact",
    )
    run_active.set_defaults(func=_cmd_run_active)
    run_active.add_argument(
        "--failure-sample-limit", type=int, default=20,
        help="Maximum diagnostic signatures to count separately; class totals stay exact",
    )

    task = commands.add_parser("task", help="Execute portable serialized DocumentStore tasks")
    task_commands = _subcommands(task, dest="task_command")
    _add_mutating_paths(
        task_commands.add_parser("execute", help="Execute one serialized task and emit one result"),
        operation="task.execute",
        func=_cmd_local_task_execute,
    )

    sink = commands.add_parser("sink", help="Verify result delivery evidence")
    sink_commands = _subcommands(sink, dest="sink_command")
    sink_verify = sink_commands.add_parser("verify", help="Verify and summarize a DeliveryReceipt")
    sink_verify.add_argument("--receipt", type=Path, required=True)
    sink_verify.add_argument("--control-root", type=Path, help="Resolve an ArtifactRef from this control repository")
    sink_verify.set_defaults(func=_cmd_sink_verify)

    release = commands.add_parser("document-release", help="Retain, commit, verify, compare, and compact releases")
    release_commands = _subcommands(release, dest="document_release_command")
    _add_mutating_paths(
        release_commands.add_parser("commit", help="Commit a reconciled local run with compare-and-swap"),
        operation="document-release.commit",
        func=_cmd_document_release_save,
    )
    _add_mutating_paths(
        release_commands.add_parser("retain", help="Keep a verified result without changing the current selection"),
        operation="document-release.retain",
        func=_cmd_document_release_save,
    )
    release_verify = release_commands.add_parser("verify", help="Verify one canonical release root")
    release_verify.add_argument("release", type=Path)
    release_verify.set_defaults(func=_cmd_document_release_verify)
    release_diff = release_commands.add_parser("diff", help="Compare two complete release roots")
    release_diff.add_argument("--older", type=Path, required=True)
    release_diff.add_argument("--newer", type=Path, required=True)
    release_diff.set_defaults(func=_cmd_document_release_diff)
    _add_mutating_paths(
        release_commands.add_parser("compact", help="Publish an equivalent compacted successor release"),
        operation="document-release.compact",
        func=_cmd_document_release_compact,
    )

    blob_store = commands.add_parser("blob-store", help="Verify immutable blobs and inventory safe collection")
    blob_commands = _subcommands(blob_store, dest="blob_store_command")
    blob_verify = blob_commands.add_parser("verify", help="Verify one immutable blob reference")
    blob_verify.add_argument("--root", type=Path, required=True)
    blob_verify.add_argument("--reference", type=Path, required=True)
    blob_verify.add_argument("--max-blob-bytes", type=int, default=8 * 1024**3)
    blob_verify.add_argument("--stream-chunk-bytes", type=int, default=1024**2)
    blob_verify.set_defaults(func=_cmd_blob_store_verify)
    blob_gc = blob_commands.add_parser("gc", help="Inventory unreferenced content-addressed objects")
    blob_gc.add_argument("--run-request", type=Path, required=True)
    blob_gc.add_argument("--retention-set", type=Path, required=True, help="JSON ArtifactRef")
    blob_gc.add_argument("--minimum-age-seconds", type=int, required=True)
    blob_gc.add_argument("--sample-limit", type=int, default=20)
    blob_gc.add_argument("--max-index-bytes", type=int, default=64 * 1024**3)
    blob_gc.add_argument("--index-cache-kib", type=int, default=8 * 1024)
    blob_gc.add_argument("--dry-run", action="store_true", required=True)
    blob_gc.set_defaults(func=_cmd_blob_store_gc)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "sample_limit") and args.sample_limit < 0:
            raise CliError("sample limit must be non-negative")
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
