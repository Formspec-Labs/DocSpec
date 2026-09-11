"""Stable verification diagnostics and schema validation messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema

DIAGNOSTIC_CODES: tuple[str, ...] = (
    # Bundle integrity: nothing below can be judged until the bytes are trusted.
    "invalid.root-syntax",
    "invalid.format",
    "invalid.identity",
    # Amendment B2: the id embeds a version, and what it embeds is now checked.
    # An identity rule, so it sits beside the identity it constrains.
    "invalid.version-binding",
    "invalid.path",
    "invalid.membership-missing",
    "invalid.membership-extra",
    "invalid.member-digest",
    "invalid.schema",
    "invalid.duplicate-identity",
    # Domain, in dependency order: you cannot judge a segment before the
    # structure it hangs off, nor structure before the representation it
    # indexes, nor a representation before the capture it was extracted from.
    "invalid.source-catalog-pin",
    "invalid.disposition",
    # The three Decision 0001 named and nothing implemented (amendment B4).
    # `invalid.comment-selection` is ordered immediately after
    # `invalid.disposition` because that decision says so; the other two follow
    # it, before the capture they gate.
    "invalid.comment-selection",
    "invalid.attachment-accounting",
    "invalid.retention-floor",
    "invalid.capture",
    "invalid.representation",
    "invalid.structure",
    "invalid.segment",
    "invalid.coverage",
    "invalid.join",
    "invalid.set-digest",
    "invalid.counts",
)


CODE_PRECEDENCE: dict[str, int] = {
    code: index for index, code in enumerate(DIAGNOSTIC_CODES)
}


@dataclass(frozen=True)
class VerificationIssue:
    """One deterministic conformance diagnostic."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.path}: {self.message}"


@dataclass(frozen=True)
class VerificationResult:
    """The ordered result of verifying one materialized bundle."""

    release_id: str | None
    issues: tuple[VerificationIssue, ...]

    @property
    def first(self) -> VerificationIssue | None:
        if not self.issues:
            return None
        return min(
            self.issues, key=lambda issue: CODE_PRECEDENCE.get(issue.code, 10_000)
        )

    @property
    def code(self) -> str:
        first = self.first
        return "valid" if first is None else first.code

    @property
    def path(self) -> str | None:
        first = self.first
        return None if first is None else first.path

    @property
    def valid(self) -> bool:
        return not self.issues


# ─── Verification ──────────────────────────────────────────────────────


def _issue(issues: list[VerificationIssue], code: str, path: str, message: str) -> None:
    issues.append(VerificationIssue(code=code, path=path, message=message))


def _schema_issues(value: Any, schema: Mapping[str, Any], *, path: str) -> list[VerificationIssue]:
    return _validator_issues(jsonschema.Draft202012Validator(schema), value, path=path)


def _validator_issues(
    validator: jsonschema.Draft202012Validator, value: Any, *, path: str
) -> list[VerificationIssue]:
    """The same check, against a validator the caller may reuse across rows.

    Compiling a schema is the expensive half of validating one small record, and
    a tabular member of this format is one schema and a quarter of a million
    rows. The rule is unchanged: the same validator, the same errors, the same
    order.
    """

    issues: list[VerificationIssue] = []
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path)):
        suffix = "".join(f"/{part}" for part in error.path)
        _issue(issues, "invalid.schema", f"{path}{suffix}", error.message)
    return issues
