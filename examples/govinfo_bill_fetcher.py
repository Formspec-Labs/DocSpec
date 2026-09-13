"""Connect one explicitly selected SpicyDocs bill XML to DocSpec's fetcher port."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from spicy_docs.sources.congress.bill_acquisition import BillAcquirer, BillStatusAcquisition
from spicy_docs.sources.congress.bill_status import select_bill_xml

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream
from examples.provider_identity import provider_installation
from examples.dataset_example_support import capture_facts, retain_refusal


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BillContentFetcher:
    """Use the provider's bounded XML acquisition without selecting another version."""

    acquirer: BillAcquirer
    status: BillStatusAcquisition
    package_id: str
    evidence_root: Path
    clock: Callable[[], datetime] = _utc_now
    downloader_id = "docspec.example.GovInfoBillContentFetcher/v1"

    @property
    def locator(self) -> str:
        return select_bill_xml(self.status.status, self.package_id)[1].url

    @property
    def configuration_digest(self) -> str:
        return identity_digest({
            "provider": provider_installation(), "billStatusSha256": self.status.capture.sha256,
            "packageId": self.package_id, "format": "application/xml",
            "budget": asdict(self.acquirer.budget),
        })

    def fetch(self, candidate: CandidateFile, *, max_bytes: int, task_id: str, attempt_id: str) -> FetchStream:
        if candidate.locator != self.locator or candidate.media_type != "application/xml":
            raise IntegrityError("bill candidate differs from the explicitly selected package XML")
        receipt_name = "bill-text-" + identity_digest({"taskId": task_id, "attemptId": attempt_id}).split(":")[1]
        started_at = self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        try:
            result = self.acquirer.acquire_text(self.status, package_id=self.package_id, max_bytes=max_bytes)
        except Exception as error:
            retain_refusal(error, self.evidence_root / f"{receipt_name}-refusal.json")
            raise
        capture = result.capture
        if capture.requested_url != self.locator:
            raise IntegrityError("provider returned a different bill XML locator")
        receipt = {
            "identity": asdict(result.identity), "capture": capture_facts(capture),
            "requestCount": result.request_count, "budget": asdict(result.budget),
            "taskId": task_id, "attemptId": attempt_id,
            "acquisitionStartedAt": started_at,
        }
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        (self.evidence_root / f"{receipt_name}.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return FetchStream(
            FetchMetadata(self.downloader_id, self.configuration_digest,
                          None, started_at, task_id, attempt_id),
            iter((capture.body,)),
        )
