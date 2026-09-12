"""Final failure evidence survives unordered delivery and successful retries."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace

import pytest

from docspec.domain.content import AcquisitionDisposition, CandidateFile, SourceItem
from docspec.domain.delivery import iter_delivery_records, verify_logical_release_layers
from docspec.domain.dispositions import DISPOSITION_SCHEMA_ID, disposition_payload, parse_disposition_payload
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, FailureClass, FailureRecord
from docspec.domain.plans import WorkLimits
from docspec.errors import IntegrityError
from docspec.runtime import stage_policy


TRANSIENT = FailureRecord(FailureClass.TRANSIENT_EXTERNAL, "test.timeout", "temporary", 3, True)
PERMANENT = FailureRecord(FailureClass.DETERMINISTIC_INPUT, "test.input", "permanent", 1, False)


def _entry(failures, disposition=AcquisitionDisposition.ACCEPTED_FAILURE):
    source = SourceItem("fixture:item", "1", (CandidateFile("primary", "fixture.txt", "text/plain"),))
    return replace(
        DocumentEntry.create(source, ChangeKind.ADDED, stage_policy(stop_after="capture")),
        failures=failures, disposition=disposition,
    )


def _layers(entry):
    store = DocumentStore.planned(
        plan_id="fixture:plan", logical_partition="fixture:partition", entries=(entry,),
        limits=WorkLimits(1, 1000, 1, 1, 1, 1000, 60, 3),
    )
    layers = defaultdict(list)
    for record in iter_delivery_records(store):
        if record.layer_kind == "dispositions":
            assert record.schema.schema_id == DISPOSITION_SCHEMA_ID
        layers[record.layer_kind].append(record.to_record())
    return dict(layers)


@pytest.mark.parametrize("failures", [(TRANSIENT, PERMANENT), (PERMANENT, TRANSIENT)])
def test_final_failure_comes_from_entry_order_not_attempt_number_or_layer_order(failures):
    entry = _entry(failures)
    layers = _layers(entry)
    layers["failures"].reverse()
    assert layers["dispositions"][0]["payload"]["terminalFailure"] == failures[-1].to_dict()
    assert parse_disposition_payload(disposition_payload(entry)) == (entry.requested_stages, failures[-1])
    verify_logical_release_layers(layers)


def test_successful_retry_keeps_history_without_a_terminal_failure():
    entry = _entry((TRANSIENT,), AcquisitionDisposition.CAPTURED)
    layers = _layers(entry)
    assert layers["dispositions"][0]["payload"]["terminalFailure"] is None
    assert len(layers["failures"]) == 1
    verify_logical_release_layers(layers)


def test_failed_entry_cannot_publish_without_its_final_failure():
    with pytest.raises(IntegrityError, match="requires its terminal failure"):
        disposition_payload(_entry(()))


@pytest.mark.parametrize("damage", ["missing", "unknown-class", "wrong-retryability", "success-with-failure"])
def test_disposition_refuses_missing_or_contradictory_final_classification(damage):
    payload = disposition_payload(_entry((PERMANENT,)))
    if damage == "missing":
        payload["terminalFailure"] = None
    elif damage == "unknown-class":
        payload["terminalFailure"]["failureClass"] = "invented"
    elif damage == "wrong-retryability":
        payload["terminalFailure"]["retryable"] = True
    else:
        payload["disposition"] = "captured"
    with pytest.raises(IntegrityError):
        parse_disposition_payload(payload)


@pytest.mark.parametrize("damage", ["missing", "other-entry", "other-source", "different-failure"])
def test_terminal_failure_must_have_matching_source_and_attempt_evidence(damage):
    layers = deepcopy(_layers(_entry((PERMANENT,))))
    row = layers["failures"][0]
    if damage == "missing":
        layers["failures"] = []
    elif damage == "other-entry":
        row["payload"]["entryId"] = "another:entry"
    elif damage == "other-source":
        row["sourceItemId"] = "another:source"
    else:
        row["payload"].update(TRANSIENT.to_dict())
    with pytest.raises(IntegrityError):
        verify_logical_release_layers(layers)
