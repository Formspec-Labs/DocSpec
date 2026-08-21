from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from docspec.adapters.sinks import DurableDatasetSink, HybridResultSink, ReturnedResultSink
from docspec.domain.delivery import iter_delivery_records
from docspec.domain.profiles import ProfileRole
from docspec.domain.storage import PartitionPolicy
from docspec.profile_registry import ProfileRegistry, RegisteredProfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_record_contract = importlib.import_module("tests.conformance.test_record_storage_contract")
_sink_helpers = importlib.import_module("tests.test_result_sinks_and_recovery")
_helpers = importlib.import_module("tests.helpers")
_terminal_store = _sink_helpers._terminal_store
_RecordingReceiver = _sink_helpers._RecordingReceiver
_clock = _sink_helpers._clock
artifact = _helpers.artifact

POLICY = PartitionPolicy("source-item-sha256-v1", 8)


def _record_storage(root: Path):
    registered = _record_contract._registered_record_profiles()[0]
    return _record_contract._FACTORIES[registered.description.implementation_id](registered, root)


def _durable(registered: RegisteredProfile, root: Path):
    storage = _record_storage(root)
    sink = DurableDatasetSink(
        sink_id="urn:docspec:conformance:sink:durable",
        profile_id=registered.description.profile_id,
        storage=storage,
        partition_policy=POLICY,
        blob_roots=(artifact("urn:docspec:conformance:blob-root"),),
        clock=_clock,
    )
    return sink, None, storage


def _returned(registered: RegisteredProfile, root: Path):
    receiver = _RecordingReceiver()
    sink = ReturnedResultSink(
        sink_id="urn:docspec:conformance:sink:returned",
        profile_id=registered.description.profile_id,
        receiver=receiver,
        clock=_clock,
    )
    return sink, receiver, None


def _hybrid(registered: RegisteredProfile, root: Path):
    receiver = _RecordingReceiver()
    durable, _, storage = _durable(registered, root)
    sink = HybridResultSink(
        sink_id="urn:docspec:conformance:sink:hybrid",
        profile_id=registered.description.profile_id,
        durable=durable,
        receiver=receiver,
        clock=_clock,
    )
    return sink, receiver, storage


# One factory per registered result-delivery profile; a new registration
# fails the coverage check below until it joins and passes the same store.
_FACTORIES: dict[str, Callable[[RegisteredProfile, Path], tuple[Any, Any, Any]]] = {
    "docspec.result-delivery.durable-dataset.v1": _durable,
    "docspec.result-delivery.returned-results.v1": _returned,
    "docspec.result-delivery.hybrid.v1": _hybrid,
}


def _registered_delivery_profiles() -> tuple[RegisteredProfile, ...]:
    profiles = ProfileRegistry.from_directory(ROOT / "profiles").list(ProfileRole.RESULT_DELIVERY)
    assert profiles
    assert {item.description.implementation_id for item in profiles} == set(_FACTORIES), (
        "a registered result-delivery profile has no conformance factory"
    )
    return profiles


def test_every_registered_delivery_profile_passes_the_shared_store_contract(tmp_path: Path) -> None:
    """Deliver one terminal store through every registered sink profile: the
    receipts must be complete, describe the identical logical stream, replay
    to the same identities, and acknowledge under backpressure."""

    store = _terminal_store()
    expected = tuple(iter_delivery_records(store))
    expected_keys = [record.idempotency_key for record in expected]
    stream_facts: set[tuple[Any, ...]] = set()

    for registered in _registered_delivery_profiles():
        implementation_id = registered.description.implementation_id
        sink, receiver, storage = _FACTORIES[implementation_id](registered, tmp_path / implementation_id)

        if receiver is None:
            receipt = sink.deliver(store, iter(expected))
        else:

            def acknowledged_stream() -> Iterator[Any]:
                for index, record in enumerate(expected):
                    assert len(receiver.attempted) == index, (
                        "each record must be acknowledged before the next is pulled"
                    )
                    yield record

            receipt = sink.deliver(store, acknowledged_stream())
            assert receiver.attempted == expected_keys
            assert set(receiver.accepted) == set(expected_keys)
            assert receipt.returned_result == receiver.result

        assert receipt.sink_id == sink.sink_id
        assert receipt.profile_id == registered.description.profile_id
        assert receipt.delivered_entry_count == len(store.entries)
        assert receipt.record_count == len(expected)
        assert receipt.accepted_record_count == receipt.record_count
        assert (receipt.rejected_record_count, receipt.retried_record_count, receipt.undelivered_record_count) == (
            0,
            0,
            0,
        )
        stream_facts.add(
            (
                receipt.record_count,
                receipt.byte_count,
                receipt.idempotency_set_digest,
                receipt.delivered_entry_population_digest,
                receipt.final_verdict,
            )
        )

        if receipt.layers:
            assert storage is not None
            delivered_rows = []
            for layer in receipt.layers:
                storage.verify(layer)
                delivered_rows.extend(storage.stream(layer))
            assert sorted(row["recordId"] for row in delivered_rows) == sorted(
                record.record_id for record in expected
            )
            assert receipt.blob_roots

        replay = sink.deliver(store, iter(expected))
        assert replay.receipt_id == receipt.receipt_id
        assert replay.layers == receipt.layers, "a replayed delivery must land on the same immutable layers"

    assert len(stream_facts) == 1, "every registered sink must describe the identical logical delivery"


def test_interrupted_acknowledgement_replays_to_the_same_receipt(tmp_path: Path) -> None:
    store = _terminal_store()
    expected = tuple(iter_delivery_records(store))
    receiver_backed = [
        registered
        for registered in _registered_delivery_profiles()
        if _FACTORIES[registered.description.implementation_id](registered, tmp_path / "probe")[1] is not None
    ]
    assert receiver_backed, "the registry must offer at least one acknowledged delivery profile"

    for registered in receiver_backed:
        implementation_id = registered.description.implementation_id
        sink, receiver, _ = _FACTORIES[implementation_id](registered, tmp_path / implementation_id)
        receiver.fail_on_attempt = 2

        with pytest.raises(ConnectionError):
            sink.deliver(store, iter(expected))
        interrupted_attempts = list(receiver.attempted)
        assert len(interrupted_attempts) == 2

        replay = sink.deliver(store, iter(expected))
        assert receiver.attempted == interrupted_attempts + [record.idempotency_key for record in expected]
        assert set(receiver.accepted) == {record.idempotency_key for record in expected}
        assert replay.record_count == len(expected)
        assert replay.accepted_record_count == len(expected)
