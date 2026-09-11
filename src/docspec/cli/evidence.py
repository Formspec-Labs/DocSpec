"""DocSpec command evidence: run and delivery receipt inspection."""

from __future__ import annotations

import argparse

from docspec.cli.common import _load_receipt_value
from docspec.cli_io import (
    CliError,
)
from docspec.cli_io import (
    emit as _emit,
)
from docspec.domain.receipts import DeliveryReceipt, RunReceipt


def _cmd_run_status(args: argparse.Namespace) -> int:
    receipt = _load_receipt_value(
        args.receipt,
        control_root=args.control_root,
        label="run receipt",
        parser=RunReceipt.from_dict,
        inline_format="docspec-run-receipt",
    )
    failure_counts = receipt.failures.get("counts")
    if not isinstance(failure_counts, dict) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in failure_counts.values()
    ):
        raise CliError("run receipt failure counts have an invalid shape")
    _emit(
        {
            "format": "docspec-run-status",
            "formatVersion": "1.0",
            "runId": receipt.run_id,
            "status": "completed",
            "stateful": receipt.stateful,
            "storeCount": receipt.store_count,
            "counts": receipt.counts,
            "failureCount": sum(failure_counts.values()),
            "completedAt": receipt.completed_at,
            "verificationScope": "run-receipt-structure",
            "verdict": "structurally-valid",
        }
    )
    return 0


def _cmd_sink_verify(args: argparse.Namespace) -> int:
    receipt = _load_receipt_value(
        args.receipt,
        control_root=args.control_root,
        label="delivery receipt",
        parser=DeliveryReceipt.from_dict,
        inline_format="docspec-delivery-receipt",
    )
    _emit(
        {
            "format": "docspec-sink-verification",
            "formatVersion": "1.0",
            "receiptId": receipt.receipt_id,
            "sinkId": receipt.sink_id,
            "profileId": receipt.profile_id,
            "storeId": receipt.store_id,
            "deliveredEntryCount": receipt.delivered_entry_count,
            "deliveredEntryPopulationDigest": receipt.delivered_entry_population_digest,
            "recordCount": receipt.record_count,
            "byteCount": receipt.byte_count,
            "acceptedRecordCount": receipt.accepted_record_count,
            "rejectedRecordCount": receipt.rejected_record_count,
            "retriedRecordCount": receipt.retried_record_count,
            "undeliveredRecordCount": receipt.undelivered_record_count,
            "finalVerdict": receipt.final_verdict.value,
            "layerCount": len(receipt.layers),
            "returnedResult": None if receipt.returned_result is None else receipt.returned_result.to_dict(),
            "verificationScope": "delivery-receipt-structure",
            "verdict": "structurally-valid",
        }
    )
    return 0
