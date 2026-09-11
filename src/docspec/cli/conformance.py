"""DocSpec command conformance: conformance execution and reports."""

from __future__ import annotations

import argparse

from docspec.cli_io import (
    emit as _emit,
)
from docspec.conformance import run_conformance, summarize_report


def _cmd_conformance_run(args: argparse.Namespace) -> int:
    report = run_conformance(
        source_root=args.root,
        specification_path=args.specification,
        matrix_path=args.matrix,
        output_path=args.output,
        conformance_class=args.conformance_class,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(summarize_report(args.output))
    return 0 if report["verdict"] == "pass" else 1


def _cmd_conformance_report(args: argparse.Namespace) -> int:
    summary = summarize_report(args.report)
    _emit(summary)
    return 0 if summary["verdict"] == "pass" else 1
