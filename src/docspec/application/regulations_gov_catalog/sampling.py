"""Deterministic Regulations.gov sampling over the bounded policy workspace."""

from __future__ import annotations

import bisect
import hashlib
import math
from dataclasses import dataclass
from typing import Any

from docspec.application.catalog_policy import (
    text_value as _text,
    utc_instant_date_value as _instant_date,
)
from docspec.domain.identity import closed_mapping
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    CatalogPolicyWorkspace,
)

from .indexed_rows import (
    _DOCUMENT_INDEX,
    _stored_row,
)
from .records import (
    _record_data,
)

_SAMPLE_ORDER = "regulations-gov-catalog/sample-order"

_SAMPLE_COUNTS = "regulations-gov-catalog/sample-counts"

_SAMPLE_DRAWN = "regulations-gov-catalog/sample-drawn"

_SAMPLE_DETAILS = "regulations-gov-catalog/sample-details"

_UNKNOWN_STRATUM_PART = "unknown"


@dataclass(frozen=True, slots=True)
class RegulationsGovSamplePolicy:
    """Deterministic stratified draw retained from the proven source policy."""

    seed: str
    per_partition_limit: int

    def __post_init__(self) -> None:
        """Require a nonempty seed and a positive integer per-partition limit."""
        if not isinstance(self.seed, str) or not self.seed:
            raise ValueError("Regulations.gov sample seed must be nonempty")
        if (
            isinstance(self.per_partition_limit, bool)
            or not isinstance(self.per_partition_limit, int)
            or self.per_partition_limit < 1
        ):
            raise ValueError("Regulations.gov sample per-partition limit must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocation": "rank-over-sqrt-stratum-size",
            "orderHash": "md5-document-id-colon-seed",
            "partitionBy": "documentType",
            "perPartitionLimit": self.per_partition_limit,
            "seed": self.seed,
            "stratifyBy": ["agencyId", "publicationYear"],
        }

    @classmethod
    def from_dict(cls, value: object) -> RegulationsGovSamplePolicy:
        """Rebuild the installed sample policy from a member, refusing any difference."""
        item = closed_mapping(
            value,
            {
                "allocation",
                "orderHash",
                "partitionBy",
                "perPartitionLimit",
                "seed",
                "stratifyBy",
            },
            "Regulations.gov sample policy",
            error=ValueError,
        )
        policy = cls(item["seed"], item["perPartitionLimit"])
        if item != policy.to_dict():
            raise ValueError("Regulations.gov sample policy differs from the installed policy")
        return policy


def _draw_document_sample(workspace: CatalogPolicyWorkspace, *, sample: RegulationsGovSamplePolicy | None) -> None:
    """Draw the deterministic stratified sample into the workspace's order, count, detail and drawn indexes."""
    if sample is None:
        raise AssertionError("sample staging requires a sample policy")
    for value in workspace.iter_ordered(_DOCUMENT_INDEX):
        record, _ = _stored_row(value)
        source_item_id = str(record["sourceRecordId"])
        _, attributes = _record_data(record, expected_type="documents")
        if attributes.get("withdrawn") is True:
            continue
        document_type, _ = _text(attributes.get("documentType"))
        agency_id, _ = _text(attributes.get("agencyId"))
        publication_date, _ = _instant_date(attributes.get("postedDate"))
        partition = document_type or _UNKNOWN_STRATUM_PART
        agency = agency_id or _UNKNOWN_STRATUM_PART
        year = publication_date[:4] if publication_date is not None else _UNKNOWN_STRATUM_PART
        order_hash = hashlib.md5(
            f"{source_item_id}:{sample.seed}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        workspace.put(
            _SAMPLE_ORDER,
            (partition, agency, year, order_hash, source_item_id),
            {
                "agency": agency,
                "documentId": source_item_id,
                "orderHash": order_hash,
                "partition": partition,
                "sourceItemId": source_item_id,
                "year": year,
            },
        )

    previous_group: tuple[str, str, str] | None = None
    count = 0
    for value in workspace.iter_ordered(_SAMPLE_ORDER):
        group = (
            str(value["partition"]),
            str(value["agency"]),
            str(value["year"]),
        )
        if previous_group is not None and group != previous_group:
            workspace.put(
                _SAMPLE_COUNTS,
                previous_group,
                {"count": count},
            )
            count = 0
        previous_group = group
        count += 1
    if previous_group is not None:
        workspace.put(_SAMPLE_COUNTS, previous_group, {"count": count})

    current_partition: str | None = None
    current_group: tuple[str, str, str] | None = None
    rank = 0
    selected: list[tuple[float, str, str, str]] = []

    def flush() -> None:
        for _, _, _, source_item_id in selected:
            workspace.put(
                _SAMPLE_DRAWN,
                (source_item_id,),
                {"sourceItemId": source_item_id},
            )

    for value in workspace.iter_ordered(_SAMPLE_ORDER):
        partition = str(value["partition"])
        group = (partition, str(value["agency"]), str(value["year"]))
        if current_partition is not None and partition != current_partition:
            flush()
            selected = []
        if group != current_group:
            current_group = group
            rank = 0
        current_partition = partition
        rank += 1
        count_value = workspace.get(_SAMPLE_COUNTS, group)
        if (
            count_value is None
            or set(count_value) != {"count"}
            or isinstance(count_value["count"], bool)
            or not isinstance(count_value["count"], int)
            or count_value["count"] < rank
        ):
            raise IntegrityError("catalog sample stratum count is invalid")
        workspace.put(
            _SAMPLE_DETAILS,
            (str(value["sourceItemId"]),),
            {
                "partition": partition,
                "stratum": [str(value["agency"]), str(value["year"])],
                "orderHash": str(value["orderHash"]),
                "rank": rank,
                "stratumSize": count_value["count"],
            },
        )
        candidate = (
            rank / math.sqrt(count_value["count"]),
            str(value["orderHash"]),
            str(value["documentId"]),
            str(value["sourceItemId"]),
        )
        bisect.insort(selected, candidate)
        if len(selected) > sample.per_partition_limit:
            selected.pop()
    if current_partition is not None:
        flush()


def _sampling_result(
    source_item_id: str,
    *,
    withdrawn: bool,
    sample_drawn: bool | None,
    workspace: CatalogPolicyWorkspace,
    sample: RegulationsGovSamplePolicy | None,
) -> dict[str, Any]:
    """Describe one document's sampling outcome, requiring details when a sample is configured."""
    if sample is None:
        return {
            "frameAdmitted": not withdrawn,
            "partition": None if withdrawn else "all",
            "stratum": [] if withdrawn else ["all"],
            "orderHash": None,
            "rank": None,
            "stratumSize": None,
            "allocationMethod": "all",
            "limit": None,
            "drawn": not withdrawn,
        }
    if withdrawn:
        return {
            "frameAdmitted": False,
            "partition": None,
            "stratum": [],
            "orderHash": None,
            "rank": None,
            "stratumSize": None,
            "allocationMethod": "rank-over-sqrt-stratum-size",
            "limit": sample.per_partition_limit,
            "drawn": False,
        }
    details = workspace.get(_SAMPLE_DETAILS, (source_item_id,))
    if details is None or set(details) != {
        "partition",
        "stratum",
        "orderHash",
        "rank",
        "stratumSize",
    }:
        raise IntegrityError("catalog sample details are missing or malformed")
    if sample_drawn is None:
        raise IntegrityError("catalog sample decision is missing")
    return {
        "frameAdmitted": True,
        **dict(details),
        "allocationMethod": "rank-over-sqrt-stratum-size",
        "limit": sample.per_partition_limit,
        "drawn": sample_drawn,
    }
