"""Connect one explicitly selected SpicyDocs bill XML to DocSpec's fetcher port."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import distribution
from pathlib import Path

from spicy_docs.sources.congress.bill_acquisition import BillAcquirer, BillStatusAcquisition
from spicy_docs.sources.congress.bill_status import select_bill_xml
from spicy_docs.transport.capture import CapturedBodyResponse

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest, sha256_digest
from docspec.errors import IntegrityError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream


def _utc_now() -> datetime:
    return datetime.now(UTC)


def provider_installation() -> dict:
    """Pin installed provider bytes even when the installer omits a wheel hash."""
    installed = distribution("spicy-docs")
    direct = json.loads(installed.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        raise ValueError("install a SpicyDocs wheel; this example does not support editable provider installs")
    archive = direct.get("archive_info", {})
    wheel_hash = archive.get("hashes", {}).get("sha256")
    files = {str(path): sha256_digest(installed.locate_file(path).read_bytes())
             for path in installed.files or ()
             if str(path).startswith("spicy_docs/") and not str(path).endswith(".pyc")}
    if not files:
        raise ValueError("installed SpicyDocs package has no recorded source files")
    return {"package": "spicy-docs", "version": installed.version,
            "wheelSha256": "sha256:" + wheel_hash if wheel_hash else None,
            "installedFilesSha256": identity_digest(files)}


def capture_facts(capture: CapturedBodyResponse) -> dict:
    return {
        "requestedUrl": capture.requested_url, "resolvedUrl": capture.resolved_url,
        "statusCode": capture.status_code, "contentType": capture.content_type,
        "observedAt": capture.observed_at, "byteSize": capture.byte_size, "sha256": capture.sha256,
    }


def retain_refusal(error: Exception, destination: Path) -> None:
    """Keep bounded publisher evidence before normal exception handling continues."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"errorType": type(error).__name__, "message": str(error),
               "acquisition": getattr(error, "bill_acquisition", None)}
    refused = getattr(error, "refused_response", None)
    if refused is not None:
        receipt["response"] = {"requestKey": refused.request_key, "stage": refused.stage,
                               "mediaType": refused.media_type, "unavailableReason": refused.unavailable_reason,
                               "observedByteSize": refused.observed_byte_size}
        if refused.response_bytes is not None:
            body = destination.with_suffix(".body")
            body.write_bytes(refused.response_bytes)
            receipt["response"].update({"bodyFile": body.name, "sha256": sha256_digest(refused.response_bytes)})
    destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


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
                          self.package_id + ":" + capture.sha256, started_at, task_id, attempt_id),
            iter((capture.body,)),
        )
