"""The typed dependency boundary of an active-result consumer export."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.processors import ProcessorRequest
from docspec.domain.references import ArtifactRef, BlobRef


def row_references(kind: str, row: Mapping[str, Any]) -> Iterator[ArtifactRef | BlobRef]:
    payload = row["payload"]
    if kind == "files":
        yield CapturedFile.from_dict(payload).blob
    elif kind == "representations":
        yield Representation.from_dict(payload).blob
    elif kind == "segments":
        yield Segment.from_dict(payload).content
    elif kind == "receipts":
        yield ArtifactRef.from_dict(payload["artifact"])


def evidence_references(value: Mapping[str, Any]) -> tuple[ArtifactRef, ...]:
    """Follow declared invocation inputs/outputs, not arbitrary provider JSON.

    Owning plans are embedded to interpret processor evidence. Their source
    catalogs, bases and machine profiles remain external provenance pins.
    """
    if value.get("format") != "docspec-processor-invocation-receipt":
        return ()
    request = ProcessorRequest.from_dict(value["request"])
    return (request.plan, ArtifactRef.from_dict(value["result"]), *request.prerequisite_results)
