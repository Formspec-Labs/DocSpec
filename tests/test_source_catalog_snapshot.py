"""Normative catalog contents, immutable identity, and verified blob reuse."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.catalog_artifact import accounting as catalog_accounting
from docspec.adapters.catalog_artifact import rules as catalog_rules
from docspec.adapters.catalog_artifact.digests import requested_universe_set_digest, selected_source_set_digest
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.source_catalog_store import staging as catalog_staging
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.source_catalog import CatalogDisposition
from docspec.errors import IntegrityError
from tests.support.source_catalog import _FEDERAL_REGISTER_SOURCE, FakeSource, description, producer, record, renditions
from tests.support.source_catalog_builds import (
    build,
    interpretation_result,
    normalization_fields,
)


def test_builds_and_streams_one_complete_normative_snapshot(tmp_path: Path) -> None:
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    store, result = build(tmp_path, source)

    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)

    assert snapshot.summary == result.summary
    assert snapshot.summary.logical_id == result.reference.catalog_id
    assert snapshot.summary.artifact_digest == result.reference.digest
    assert snapshot.summary.item_count == 1
    assert snapshot.summary.disposition_counts == {
        "selected": 1,
        "excluded": 0,
        "deleted": 0,
        "unavailable": 0,
        "failed": 0,
    }
    assert snapshot.summary.requested_universe_set_digest == requested_universe_set_digest(
        1,
        iter(("2026-00001",)),
    )
    assert snapshot.summary.selected_source_set_digest == selected_source_set_digest(
        1,
        iter((("2026-00001", "2026-00001"),)),
    )
    assert snapshot.summary.selection_policy["policyId"] == ("urn:docspec:catalog-policy:federal-register:1")
    assert snapshot.summary.partition_policy["bucketCount"] == 64
    assert snapshot.summary.join_coverage == ()
    assert set(snapshot.summary.diagnostic_digests) == {
        "normalizedFieldsDigest",
        "joinedFieldsDigest",
        "dispositionsDigest",
        "reasonsDigest",
        "interpretationsDigest",
        "renditionChoicesDigest",
    }
    item = next(snapshot.items)
    assert item.source_item_id == "2026-00001"
    assert item.disposition is CatalogDisposition.SELECTED
    assert item.normalized_metadata["regulationIdentifierNumbers"] == ("2060-AV12",)
    assert item.source_native_facts[0]["fields"]["document_number"] == "2026-00001"
    assert [value.media_type for value in item.candidate_renditions] == ["text/html"]
    expected_policy_digest = FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).policy_digest
    assert all(value["policyDigest"] == expected_policy_digest for value in item.interpretations)
    assert [value["interpretationKind"] for value in item.interpretations] == [
        "exact-join",
        "normalization",
        "rendition-preference",
        "sampling",
        "selection",
        "topic-recovery",
    ]
    assert interpretation_result(item, "exact-join") == {"joins": ()}
    assert interpretation_result(item, "sampling") == {
        "frameAdmitted": True,
        "partition": "all",
        "stratum": ("all",),
        "orderHash": None,
        "rank": None,
        "stratumSize": None,
        "allocationMethod": "all",
        "limit": None,
        "drawn": True,
    }
    fields = normalization_fields(item)
    assert list(fields) == [
        "title",
        "agencies",
        "documentType",
        "publicationDate",
        "lastUpdatedDate",
        "docketIds",
        "regulationIdentifierNumbers",
        "commentCloseDate",
        "language",
        "sourceUrl",
    ]
    assert fields["lastUpdatedDate"]["outcome"] == "absent"
    assert fields["language"]["valueSource"] == "policy"
    assert interpretation_result(item, "selection")["decisions"] == (
        {
            "decisionId": "required-metadata",
            "outcome": "pass",
            "disposition": None,
            "reasonCode": None,
            "reason": None,
        },
        {
            "decisionId": "candidate-rendition",
            "outcome": "pass",
            "disposition": None,
            "reasonCode": None,
            "reason": None,
        },
    )
    processing = item.to_processing_item()
    assert processing.item_id == item.source_item_id
    assert processing.metadata["sourceCatalogRow"] == item.to_dict()
    with pytest.raises(StopIteration):
        next(snapshot.items)

    processing_snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)
    assert processing_snapshot.summary.disposition_counts == {
        "selected": 1,
        "excluded": 0,
        "deleted": 0,
        "unavailable": 0,
        "failed": 0,
    }
    assert [value.to_processing_item().item_id for value in processing_snapshot.items] == ["2026-00001"]


def test_identity_is_deterministic_and_one_row_change_moves_it(tmp_path: Path) -> None:
    initial = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    _, first = build(tmp_path / "first", initial)
    _, repeated = build(tmp_path / "repeated", initial)
    changed_record = record("2026-00001")
    changed_record["record"]["title"] = "Changed title"
    _, changed = build(
        tmp_path / "changed",
        FakeSource(
            replace(description(), source_state_digest="sha256:" + "d" * 64),
            (changed_record,),
            renditions("2026-00001"),
        ),
    )

    assert repeated.reference == first.reference
    assert changed.reference.catalog_id != first.reference.catalog_id
    assert changed.reference.digest != first.reference.digest


def test_multipart_successor_reuses_unchanged_blob_refs_and_writes_only_changed_partition(
    tmp_path: Path,
) -> None:
    identities_by_partition: dict[str, str] = {}
    for index in range(1, 100):
        identity = f"2026-{index:05d}"
        identities_by_partition.setdefault(catalog_rules._partition_id(identity), identity)
        if len(identities_by_partition) == 3:
            break
    identities = tuple(sorted(identities_by_partition.values()))
    assert len(identities) == 3

    initial_source = FakeSource(
        description(),
        tuple(record(identity) for identity in identities),
        tuple(value for identity in identities for value in renditions(identity)),
    )
    store, initial = build(tmp_path, initial_source)
    initial_root = tmp_path / initial.reference.digest.removeprefix("sha256:")
    initial_receipt = json.loads((initial_root / "catalog-build-receipt.json").read_text())
    initial_partitions = {value["partitionId"]: value for value in initial_receipt["partitions"]}

    assert len(initial_partitions) == 3
    assert initial.summary.partitions == tuple(initial_partitions)
    assert initial_receipt["byteMeasurements"] == {
        "payloadBytesRead": sum(value["byteSize"] for value in initial_partitions.values()),
        "payloadBytesReused": 0,
        "payloadBytesWritten": sum(value["byteSize"] for value in initial_partitions.values()),
        "publicationBytesWritten": sum(path.stat().st_size for path in initial_root.rglob("*") if path.is_file()),
    }
    assert initial_receipt["joinCoverage"] == []
    for name in (
        "normalizedFieldsDigest",
        "joinedFieldsDigest",
        "dispositionsDigest",
        "reasonsDigest",
        "interpretationsDigest",
        "renditionChoicesDigest",
    ):
        assert initial_receipt[name].startswith("sha256:")

    changed_identity = identities[0]
    changed_record = record(changed_identity)
    changed_record["record"]["title"] = "Changed title"
    changed_records = tuple(
        changed_record if identity == changed_identity else record(identity) for identity in identities
    )
    changed_description = replace(
        description(),
        logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "d" * 64,
        artifact_digest="sha256:" + "d" * 64,
        source_state_digest="sha256:" + "e" * 64,
    )
    _, successor = build(
        tmp_path,
        FakeSource(
            changed_description,
            changed_records,
            tuple(value for identity in identities for value in renditions(identity)),
        ),
    )
    successor_root = tmp_path / successor.reference.digest.removeprefix("sha256:")
    successor_receipt = json.loads((successor_root / "catalog-build-receipt.json").read_text())
    successor_partitions = {value["partitionId"]: value for value in successor_receipt["partitions"]}
    changed_partition = catalog_rules._partition_id(changed_identity)

    assert successor.reference.catalog_id != initial.reference.catalog_id
    assert successor_partitions[changed_partition]["blobRef"] != initial_partitions[changed_partition]["blobRef"]
    unchanged_partitions = set(initial_partitions) - {changed_partition}
    assert {partition_id: successor_partitions[partition_id]["blobRef"] for partition_id in unchanged_partitions} == {
        partition_id: initial_partitions[partition_id]["blobRef"] for partition_id in unchanged_partitions
    }
    assert successor_receipt["byteMeasurements"]["payloadBytesReused"] == sum(
        initial_partitions[partition_id]["byteSize"] for partition_id in unchanged_partitions
    )
    assert (
        successor_receipt["byteMeasurements"]["payloadBytesWritten"]
        == successor_partitions[changed_partition]["byteSize"]
    )
    located = tuple(
        SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(successor.reference).located_items
    )
    assert [value.item.source_item_id for value in located] == list(identities)
    assert {value.item.source_item_id: value.blob_ref for value in located} == {
        identity: successor_partitions[catalog_rules._partition_id(identity)]["blobRef"]
        for identity in identities
    }
    assert [
        item.source_item_id
        for item in SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(successor.reference).items
    ] == list(identities)


def test_physical_rebuild_preserves_logical_identity_and_moves_artifact_evidence(
    tmp_path: Path,
) -> None:
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    store, initial = build(tmp_path, source)
    _, rebuilt = build(tmp_path, source)

    assert rebuilt.reference.catalog_id == initial.reference.catalog_id
    assert rebuilt.reference.digest != initial.reference.digest

    initial_snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(initial.reference)
    rebuilt_snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(rebuilt.reference)
    assert initial_snapshot.summary.logical_id == rebuilt_snapshot.summary.logical_id
    assert initial_snapshot.summary.artifact_digest != rebuilt_snapshot.summary.artifact_digest
    assert tuple(initial_snapshot.items) == tuple(rebuilt_snapshot.items)

    with store.source_for(initial.reference).open("catalog-build-receipt.json") as stream:
        initial_receipt = json.load(stream)
    with store.source_for(rebuilt.reference).open("catalog-build-receipt.json") as stream:
        rebuilt_receipt = json.load(stream)

    assert initial_receipt["byteMeasurements"]["payloadBytesWritten"] > 0
    assert initial_receipt["byteMeasurements"]["payloadBytesReused"] == 0
    assert rebuilt_receipt["partitions"] == initial_receipt["partitions"]
    assert rebuilt_receipt["byteMeasurements"]["payloadBytesRead"] > 0
    assert rebuilt_receipt["byteMeasurements"]["payloadBytesWritten"] == 0
    assert rebuilt_receipt["byteMeasurements"]["payloadBytesReused"] > 0

    def chunks_must_not_be_consumed() -> Iterator[bytes]:
        raise AssertionError("verified CAS reuse must not rewrite payload bytes")
        yield b""  # pragma: no cover

    existing_partition = initial_receipt["partitions"][0]
    with store.stage() as staging:
        reused = staging.put_blob(
            existing_partition["blobRef"],
            existing_partition["byteSize"],
            chunks_must_not_be_consumed(),
        )
    assert reused.reused is True


def test_builder_verifies_existing_blob_before_reuse(tmp_path: Path) -> None:
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    _, initial = build(tmp_path, source)
    initial_root = tmp_path / initial.reference.digest.removeprefix("sha256:")
    receipt = json.loads((initial_root / "catalog-build-receipt.json").read_text())
    blob_path = tmp_path / ".blobs" / "sha256" / receipt["partitions"][0]["blobRef"].removeprefix("sha256:")
    blob_path.write_bytes(blob_path.read_bytes() + b"tamper")

    with pytest.raises(IntegrityError, match="differs from its content identity"):
        build(tmp_path, source)

    assert [path.name for path in tmp_path.iterdir() if not path.name.startswith(".")] == [
        initial.reference.digest.removeprefix("sha256:")
    ]


def test_root_publish_failure_exposes_no_artifact_and_recovers_by_blob_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_publish = catalog_staging._publish_directory_no_replace_at
    attempts = 0

    def fail_root_publication(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise IntegrityError("injected root publication failure")
        actual_publish(*args)

    monkeypatch.setattr(
        catalog_staging,
        "_publish_directory_no_replace_at",
        fail_root_publication,
    )
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))

    with pytest.raises(IntegrityError, match="injected root publication failure"):
        build(tmp_path, source)

    assert not [path for path in tmp_path.iterdir() if not path.name.startswith(".")]
    blob_paths = tuple((tmp_path / ".blobs" / "sha256").iterdir())
    assert len(blob_paths) == 1

    store, recovered = build(tmp_path, source)

    assert recovered.byte_measurements["payloadBytesWritten"] == 0
    assert recovered.byte_measurements["payloadBytesReused"] == blob_paths[0].stat().st_size
    assert (
        SourceCatalogArtifactReader(store, producer=producer()).verify_snapshot(recovered.reference)
        == recovered.summary
    )


def test_concurrent_builders_publish_only_valid_immutable_physical_outcomes(
    tmp_path: Path,
) -> None:
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(build, tmp_path, source) for _ in range(2)]
    results = []
    errors = []
    for future in futures:
        try:
            results.append(future.result())
        except IntegrityError as error:
            errors.append(error)

    assert len(results) in {1, 2}
    assert len(results) + len(errors) == 2
    assert len({result.reference.catalog_id for _, result in results}) == 1
    assert len({result.reference.digest for _, result in results}) == len(results)
    for store, result in results:
        summary = SourceCatalogArtifactReader(store, producer=producer()).verify_snapshot(result.reference)
        assert summary == result.summary
    if len(results) == 2:
        measurements = [result.byte_measurements for _, result in results]
        assert sorted(value["payloadBytesWritten"] for value in measurements)[0] == 0
        assert sorted(value["payloadBytesReused"] for value in measurements)[1] > 0


def test_refuses_unknown_boundary_fields(tmp_path: Path) -> None:
    unknown = record("2026-00001")
    unknown["surprise"] = True
    with pytest.raises(IntegrityError, match="invalid closed shape"):
        build(
            tmp_path / "unknown",
            FakeSource(description(), (unknown,), renditions("2026-00001")),
        )


def test_tampering_fails_before_a_snapshot_row_is_returned(tmp_path: Path) -> None:
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    store, result = build(tmp_path, source)
    artifact_root = tmp_path / result.reference.digest.removeprefix("sha256:")
    receipt = json.loads((artifact_root / "catalog-build-receipt.json").read_text())
    blob_ref = receipt["partitions"][0]["blobRef"]
    item_path = tmp_path / ".blobs" / "sha256" / blob_ref.removeprefix("sha256:")
    item_path.write_bytes(item_path.read_bytes() + b"{}\n")

    with pytest.raises(IntegrityError, match="source catalog artifact is invalid"):
        SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)


def test_receipt_reason_counts_must_be_ordered_distinct_and_reconciled() -> None:
    """The schema closes each row; this is the cross-section arithmetic it cannot
    express: sealed order, no repeats, and every non-selected bucket accounted for.
    """

    reconcile = catalog_accounting._reconcile_reason_counts
    counts = {"selected": 3, "excluded": 0, "deleted": 1, "unavailable": 2, "failed": 0}
    good = [
        {"disposition": "deleted", "reasonCode": "source.withdrawn-after-publication", "count": 1},
        {"disposition": "unavailable", "reasonCode": "source.no-candidate-rendition", "count": 1},
        {"disposition": "unavailable", "reasonCode": "source.publisher-withheld.other", "count": 1},
    ]

    reconcile(good, counts)
    with pytest.raises(IntegrityError, match="ordered and distinct"):
        reconcile(list(reversed(good)), counts)
    with pytest.raises(IntegrityError, match="ordered and distinct"):
        reconcile([good[0], good[0]], {**counts, "deleted": 2, "unavailable": 0})
    with pytest.raises(IntegrityError, match="do not account for every non-selected row"):
        reconcile(good[:2], counts)
    with pytest.raises(IntegrityError, match="do not account for every non-selected row"):
        reconcile(good, {**counts, "excluded": 1})
