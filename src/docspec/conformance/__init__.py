"""Executable conformance evidence for the standalone DocSpec platform."""
from docspec.conformance.runner import (
    ConformanceError,
    load_report,
    run_conformance,
    summarize_report,
)

__all__ = [
    "ConformanceError",
    "load_report",
    "run_conformance",
    "summarize_report",
]
