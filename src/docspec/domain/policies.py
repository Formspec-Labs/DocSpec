"""Pinned retry and accepted-failure policies used by every execution backend."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import identity_digest
from docspec.domain.jobs import FailureClass, FailureRecord


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_milliseconds: int = 250
    max_delay_milliseconds: int = 30_000
    jitter_basis_points: int = 2_000

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("retry max_attempts must be positive")
        if self.base_delay_milliseconds < 0 or self.max_delay_milliseconds < 0:
            raise ValueError("retry delays must be non-negative")
        if not 0 <= self.jitter_basis_points <= 10_000:
            raise ValueError("retry jitter_basis_points must be between zero and 10000")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "format": "docspec-retry-policy",
            "formatVersion": "1.0",
            "maxAttempts": self.max_attempts,
            "baseDelayMilliseconds": self.base_delay_milliseconds,
            "maxDelayMilliseconds": self.max_delay_milliseconds,
            "jitterBasisPoints": self.jitter_basis_points,
        }

    @property
    def digest(self) -> str:
        return identity_digest(self.to_dict())

    def delay_milliseconds(self, task_id: str, attempt: int) -> int:
        if attempt <= 0:
            raise ValueError("retry attempt must be positive")
        base = min(self.max_delay_milliseconds, self.base_delay_milliseconds * (2 ** (attempt - 1)))
        if base == 0 or self.jitter_basis_points == 0:
            return base
        seed = int.from_bytes(hashlib.sha256(f"{task_id}:{attempt}".encode()).digest()[:8], "big")
        signed_basis_points = (seed % (2 * self.jitter_basis_points + 1)) - self.jitter_basis_points
        return max(0, base + (base * signed_basis_points // 10_000))


@dataclass(frozen=True, slots=True)
class AcceptedFailurePolicy:
    accepted_classes: tuple[FailureClass, ...] = ()
    accepted_diagnostic_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(sorted(set(FailureClass(item) for item in self.accepted_classes), key=lambda item: item.value))
        if normalized != self.accepted_classes:
            raise ValueError("accepted failure classes must be sorted and distinct")
        codes = tuple(sorted(set(self.accepted_diagnostic_codes)))
        if codes != self.accepted_diagnostic_codes or any(not item for item in codes):
            raise ValueError("accepted diagnostic codes must be non-empty, sorted, and distinct")
        forbidden = {FailureClass.ARTIFACT_INTEGRITY, FailureClass.IMPLEMENTATION_DEFECT}
        if forbidden.intersection(self.accepted_classes):
            raise ValueError("integrity failures and implementation defects cannot be accepted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-accepted-failure-policy",
            "formatVersion": "1.0",
            "acceptedClasses": [item.value for item in self.accepted_classes],
            "acceptedDiagnosticCodes": list(self.accepted_diagnostic_codes),
        }

    @property
    def digest(self) -> str:
        return identity_digest(self.to_dict())

    def accepts(self, failure: FailureRecord) -> bool:
        return failure.failure_class in self.accepted_classes or failure.diagnostic_code in self.accepted_diagnostic_codes
