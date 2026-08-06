"""Executable conformance evidence for the standalone DocSpec platform."""

from docspec.conformance.fixtures import (
    FixtureCase,
    FixtureDistribution,
    FixtureExpectedOutcome,
    FixtureLayoutMember,
    FixtureMember,
    load_fixture_distribution,
)
from docspec.conformance.runner import (
    ConformanceError,
    load_report,
    run_conformance,
    summarize_report,
)

__all__ = [
    "ConformanceError",
    "FixtureCase",
    "FixtureDistribution",
    "FixtureExpectedOutcome",
    "FixtureLayoutMember",
    "FixtureMember",
    "load_fixture_distribution",
    "load_report",
    "run_conformance",
    "summarize_report",
]
