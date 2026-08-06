"""Bounded delivery of verified records."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from docspec.domain.jobs import DocumentStore
from docspec.domain.receipts import DeliveryReceipt

if TYPE_CHECKING:
    from docspec.domain.delivery import DeliveryRecord


class ResultSink(Protocol):
    @property
    def sink_id(self) -> str: ...

    @property
    def profile_id(self) -> str: ...

    def deliver(self, store: DocumentStore, records: Iterable[DeliveryRecord]) -> DeliveryReceipt: ...
