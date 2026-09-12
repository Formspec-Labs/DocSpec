"""Command adapters for the supported result-inspection API."""

from __future__ import annotations

import argparse
from itertools import islice
from pathlib import Path

from docspec.cli.common import _release_reference
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.cli_io import emit, read_object
from docspec.domain.references import ArtifactRef
from docspec.runtime import InspectionView, open_local_inspection


def _open_view(args: argparse.Namespace, prefix: str = "") -> InspectionView:
    def value(name: str):
        return getattr(args, prefix + name)

    arguments = _local_run_arguments(_local_run_request(value("request")))
    run_path, release_path = value("run_reference"), value("release_reference")
    return open_local_inspection(
        arguments["plan"], arguments["workspace"],
        document_release_producer=arguments["document_release_producer"],
        source_catalog_producer=arguments["source_catalog_producer"] if value("source_coverage") else None,
        run_ref=None if run_path is None else ArtifactRef.from_dict(
            read_object(run_path, label="run reference"),
        ),
        release_ref=None if release_path is None else _release_reference(release_path),
    )


def _cmd_inspect(args: argparse.Namespace) -> int:
    if args.sample_limit < 0:
        raise ValueError("sample limit must be non-negative")
    view = _open_view(args)
    if args.inspection_command == "summary":
        report = view.summary(sample_limit=args.sample_limit)
    elif args.inspection_command == "source":
        report = view.source(args.source_item_id, sample_limit=args.sample_limit)
    elif args.inspection_command == "compare":
        report = view.compare(_open_view(args, "other_"), sample_limit=args.sample_limit)
    else:
        rows = view.records(args.layer_kind, source_item_id=args.source_item_id)
        try:
            sample = list(islice(rows, args.sample_limit + 1))
        finally:
            rows.close()
        report = {
            "layerKind": args.layer_kind, "sourceItemId": args.source_item_id,
            "records": sample[:args.sample_limit], "truncated": len(sample) > args.sample_limit,
        }
    emit(report)
    return 0


def _view_arguments(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    parser.add_argument(f"--{prefix}request", type=Path, required=True,
                        help="Existing local run request supplies plan, locations, and accepted producers")
    parser.add_argument(f"--{prefix}source-coverage", action="store_true",
                        help="Admit source coverage using this request's accepted source producer")
    reference = parser.add_mutually_exclusive_group()
    reference.add_argument(f"--{prefix}run-reference", type=Path, help="Exact RunReceipt ArtifactRef JSON")
    reference.add_argument(f"--{prefix}release-reference", type=Path, help="Exact DocumentReleaseRef JSON")


def add_inspection_command(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("inspect", help="Explain saved work, retained results, and differences")
    subcommands = parser.add_subparsers(dest="inspection_command", required=True)
    for name, help_text in (
        ("summary", "Summarize work, results, failures, reuse, and recorded costs"),
        ("source", "Explain one source item with bounded evidence samples"),
        ("records", "Read a bounded sample from a retained result layer"),
        ("compare", "Compare stable inputs, settings, content, and work across two views"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        _view_arguments(command)
        command.add_argument("--sample-limit", type=int, default=20)
        if name == "compare":
            _view_arguments(command, "other-")
        if name in {"source", "records"}:
            command.add_argument("--source-item-id", required=name == "source")
        if name == "records":
            command.add_argument("--layer-kind", required=True)
        command.set_defaults(func=_cmd_inspect)
