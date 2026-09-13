"""Select one annual MODS offer and connect its provider capture to DocSpec."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from spicy_docs.sources.cfr.acquisition import CfrAcquirer, CfrEditionAcquisition
from spicy_docs.sources.cfr.annual import annual_cfr_xml_locator
from spicy_docs.sources.cfr.models import AnnualCfrSelection
from spicy_docs.sources.govinfo.mods import ModsElement, ModsRecord

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream
from examples.dataset_example_support import capture_facts, retain_refusal, write_json
from examples.provider_identity import provider_installation


def select_section(edition: CfrEditionAcquisition, selection: AnnualCfrSelection) -> tuple[ModsRecord, ModsElement]:
    """Require one publisher-stated annual XML offer for the requested section."""
    if selection.section is None or edition.selection != replace(selection, section=None):
        raise ValueError("select a section from the captured annual edition")
    locator = annual_cfr_xml_locator(selection)
    access_id = locator.rsplit("/", 1)[-1].removesuffix(".xml")
    records = [record for record in edition.metadata.constituents
               if any(node.text == access_id for node in record.fields("extension", "accessId"))]
    if len(records) != 1 or len(records[0].fields("extension", "accessId")) != 1:
        raise ValueError("annual section requires one unambiguous constituent accessId")
    record = records[0]
    offers = [node for node in record.urls if node.text == locator
              and node.attribute("displayLabel") == "XML rendition" and node.attribute("access") == "raw object"]
    if len(offers) != 1:
        raise ValueError("annual section requires one matching publisher-stated XML offer")
    return record, offers[0]


@dataclass(frozen=True)
class AnnualCfrContentFetcher:
    acquirer: CfrAcquirer
    edition: CfrEditionAcquisition
    selection: AnnualCfrSelection
    evidence_root: Path
    clock: Callable[[], datetime]
    downloader_id = "docspec.example.AnnualCfrContentFetcher/v1"

    @property
    def locator(self) -> str:
        return select_section(self.edition, self.selection)[1].text

    @property
    def configuration_digest(self) -> str:
        return identity_digest({
            "provider": provider_installation(), "modsSha256": self.edition.capture.sha256,
            "selection": asdict(self.selection), "format": "application/xml", "budget": asdict(self.acquirer.budget),
        })

    def fetch(self, candidate: CandidateFile, *, max_bytes: int, task_id: str, attempt_id: str) -> FetchStream:
        if candidate.locator != self.locator or candidate.media_type != "application/xml":
            raise IntegrityError("CFR candidate differs from the explicitly selected annual section XML")
        receipt_name = "cfr-text-" + identity_digest({"taskId": task_id, "attemptId": attempt_id}).split(":")[1]
        started_at = self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        try:
            result = self.acquirer.acquire_annual(self.selection, max_bytes=max_bytes)
        except Exception as error:
            retain_refusal(error, self.evidence_root / f"{receipt_name}-refusal.json", source="cfr")
            raise
        capture = result.capture
        if result.selection != self.selection or capture.requested_url != self.locator:
            raise IntegrityError("provider returned a different annual section")
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        write_json(self.evidence_root / f"{receipt_name}.json", {
            "selection": asdict(self.selection), "identity": asdict(result.identity), "capture": capture_facts(capture),
            "modsSha256": self.edition.capture.sha256, "requestCount": result.request_count,
            "budget": asdict(result.budget), "taskId": task_id, "attemptId": attempt_id,
            "acquisitionStartedAt": started_at,
        })
        return FetchStream(
            FetchMetadata(self.downloader_id, self.configuration_digest,
                          None, started_at, task_id, attempt_id),
            iter((capture.body,)),
        )
