from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.source_catalog import LocalJsonlSourceCatalog
from docspec.conformance.fixtures import (
    FixtureCase,
    FixtureCaseDiagnostic,
    FixtureDistribution,
    diagnostic_code_for_rejection,
    load_fixture_distribution,
)
from docspec.domain.content import SourceItem
from docspec.domain.identity import canonical_json_file_bytes, parse_canonical_json, thaw_json
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import SourceCatalogSummary


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "fixtures" / "conformance" / "core-v1"
SOURCE_CATALOG_CONTRACT = "SOURCE-CATALOG-CONTRACT"


def _fixtures() -> FixtureDistribution:
    return load_fixture_distribution(FIXTURE_ROOT)


def _member_bytes(distribution: FixtureDistribution, case: FixtureCase, target: str) -> bytes:
    payload = next(item.payload for item in case.layout if item.target == target)
    return next(member.read_bytes() for member in distribution.members if member.path == payload)


def _source_items(payload: bytes) -> Iterator[SourceItem]:
    for number, line in enumerate(payload.splitlines(), start=1):
        value = thaw_json(parse_canonical_json(line, label=f"fixture source item {number}", file_form=False))
        assert isinstance(value, dict)
        yield SourceItem.from_dict(value)


def _summary_value(summary: SourceCatalogSummary) -> dict[str, Any]:
    return {
        "baseCatalog": None if summary.base_catalog is None else summary.base_catalog.to_dict(),
        "catalogId": summary.catalog_id,
        "coverage": dict(summary.coverage),
        "itemCount": summary.item_count,
        "kind": summary.kind,
        "partitions": list(summary.partitions),
        "stateCounts": dict(summary.state_counts),
    }


def _write_positive_case(
    distribution: FixtureDistribution,
    case: FixtureCase,
    storage_root: Path,
) -> tuple[LocalJsonlSourceCatalog, SourceCatalogRef]:
    assert case.expected_value is not None
    expected: Mapping[str, Any] = case.expected_value
    catalogs = LocalJsonlSourceCatalog(storage_root)
    reference = catalogs.write(
        _source_items(_member_bytes(distribution, case, "items.jsonl")),
        kind=expected["kind"],
        base_catalog=(
            None
            if expected["baseCatalog"] is None
            else SourceCatalogRef.from_dict(expected["baseCatalog"])
        ),
        partitions=tuple(expected["partitions"]),
        coverage=expected["coverage"],
    )
    return catalogs, reference


def test_shared_fixture_distribution_is_closed_complete_and_sealed(tmp_path: Path) -> None:
    distribution = _fixtures()
    assert distribution.fixture_set_id == (
        "urn:docspec:conformance-fixture-set:v1:"
        "00698f5cee66fd06a43ed572f64cfd8eaac7fbda5f1f6c8b754613ceb78b69b5"
    )
    assert distribution.suite == "docspec-core-contracts/1.0"
    assert len(distribution.cases_for(SOURCE_CATALOG_CONTRACT)) == 11
    assert sum(case.expected_outcome.verdict == "accept" for case in distribution.cases) == 2
    assert sum(case.expected_outcome.verdict == "reject" for case in distribution.cases) == 9

    snapshot = distribution.case("source-catalog-snapshot")
    materialized = distribution.materialize(snapshot, tmp_path / "snapshot")
    assert (materialized / "catalog.json").read_bytes() == _member_bytes(distribution, snapshot, "catalog.json")
    assert (materialized / "items.jsonl").read_bytes() == _member_bytes(distribution, snapshot, "items.jsonl")
    with pytest.raises(IntegrityError, match="must not already exist"):
        distribution.materialize(snapshot, materialized)

    occupied = tmp_path / "occupied"
    occupied.symlink_to(materialized, target_is_directory=True)
    with pytest.raises(IntegrityError, match="must not already exist"):
        distribution.materialize(snapshot, occupied)
    assert occupied.is_symlink()


def test_independent_fixture_verifier_rejects_changed_extra_and_symlinked_bytes(tmp_path: Path) -> None:
    changed = tmp_path / "changed"
    shutil.copytree(FIXTURE_ROOT, changed)
    payload = changed / "payloads" / "source-catalog" / "snapshot-items.jsonl"
    payload.write_bytes(payload.read_bytes().replace(b'"expectedSize":23', b'"expectedSize":24'))
    with pytest.raises(IntegrityError, match="changed after verification"):
        load_fixture_distribution(changed)

    extra = tmp_path / "extra"
    shutil.copytree(FIXTURE_ROOT, extra)
    (extra / "undeclared.json").write_bytes(b"{}\n")
    with pytest.raises(IntegrityError, match="membership differs"):
        load_fixture_distribution(extra)

    linked = tmp_path / "linked"
    shutil.copytree(FIXTURE_ROOT, linked)
    (linked / "linked.json").symlink_to(linked / "fixture-set.json")
    with pytest.raises(IntegrityError, match="undeclared special node"):
        load_fixture_distribution(linked)

    empty = tmp_path / "empty"
    shutil.copytree(FIXTURE_ROOT, empty)
    (empty / "empty-directory").mkdir()
    with pytest.raises(IntegrityError, match="contains an empty directory"):
        load_fixture_distribution(empty)

    special = tmp_path / "special"
    shutil.copytree(FIXTURE_ROOT, special)
    if hasattr(os, "mkfifo"):
        os.mkfifo(special / "undeclared.fifo")
        with pytest.raises(IntegrityError, match="undeclared special node"):
            load_fixture_distribution(special)

    point_of_use = tmp_path / "point-of-use"
    shutil.copytree(FIXTURE_ROOT, point_of_use)
    verified = load_fixture_distribution(point_of_use)
    snapshot_member = next(member for member in verified.members if member.path.endswith("snapshot-items.jsonl"))
    snapshot_path = point_of_use / snapshot_member.path
    snapshot_path.unlink()
    snapshot_path.symlink_to(point_of_use / "fixture-set.json")
    with pytest.raises(IntegrityError, match="regular, non-symlink file"):
        snapshot_member.read_bytes()

    changed_identity = tmp_path / "changed-identity"
    shutil.copytree(FIXTURE_ROOT, changed_identity)
    manifest_path = changed_identity / "fixture-set.json"
    manifest = thaw_json(parse_canonical_json(manifest_path.read_bytes(), label="fixture manifest"))
    assert isinstance(manifest, dict)
    manifest["suite"] = "docspec-core-contracts/changed"
    manifest_path.write_bytes(canonical_json_file_bytes(manifest))
    with pytest.raises(IntegrityError, match="identity differs"):
        load_fixture_distribution(changed_identity)

    unknown_diagnostic = tmp_path / "unknown-diagnostic"
    shutil.copytree(FIXTURE_ROOT, unknown_diagnostic)
    manifest_path = unknown_diagnostic / "fixture-set.json"
    manifest = thaw_json(parse_canonical_json(manifest_path.read_bytes(), label="fixture manifest"))
    assert isinstance(manifest, dict)
    negative = next(case for case in manifest["cases"] if case["expectedOutcome"]["verdict"] == "reject")
    negative["expectedOutcome"]["failureCode"] = "DOCSPEC-UNKNOWN-DIAGNOSTIC"
    manifest_path.write_bytes(canonical_json_file_bytes(manifest))
    with pytest.raises(IntegrityError, match="unknown failure code"):
        load_fixture_distribution(unknown_diagnostic)


def test_source_catalog_producer_matches_shared_valid_fixture_bytes(tmp_path: Path) -> None:
    distribution = _fixtures()
    accepted = tuple(
        case
        for case in distribution.cases_for(SOURCE_CATALOG_CONTRACT)
        if case.expected_outcome.verdict == "accept"
    )
    assert [case.case_id for case in accepted] == ["source-catalog-change-set", "source-catalog-snapshot"]

    for case in accepted:
        catalogs, reference = _write_positive_case(distribution, case, tmp_path / case.case_id)
        assert reference.to_dict() == dict(case.input_reference)
        produced = (catalogs.root / reference.locator).parent
        assert (produced / "catalog.json").read_bytes() == _member_bytes(distribution, case, "catalog.json")
        assert (produced / "items.jsonl").read_bytes() == _member_bytes(distribution, case, "items.jsonl")


def test_source_catalog_independent_reader_uses_every_shared_exact_fixture(tmp_path: Path) -> None:
    distribution = _fixtures()
    cases = distribution.cases_for(SOURCE_CATALOG_CONTRACT)
    assert cases

    observed_diagnostics: dict[str, FixtureCaseDiagnostic] = {}
    for case in cases:
        reference = SourceCatalogRef.from_dict(case.input_reference)
        catalogs = LocalJsonlSourceCatalog(tmp_path / case.case_id)
        destination = (catalogs.root / reference.locator).parent
        distribution.materialize(case, destination)
        if case.expected_outcome.verdict == "accept":
            assert case.expected_value is not None
            assert _summary_value(catalogs.verify(reference)) == thaw_json(case.expected_value)
            assert len(tuple(catalogs.stream(reference))) == case.expected_value["itemCount"]
            continue
        assert case.expected_outcome.failure_code is not None
        with pytest.raises(IntegrityError) as rejection:
            catalogs.verify(reference)
        actual_code = diagnostic_code_for_rejection(case, rejection.value)
        assert actual_code is case.expected_outcome.failure_code
        observed_diagnostics[case.case_id] = actual_code

    assert observed_diagnostics == {
        "source-catalog-broken-change-ancestry": FixtureCaseDiagnostic.SOURCE_CATALOG_BROKEN_ANCESTRY,
        "source-catalog-digest-mismatch": FixtureCaseDiagnostic.SOURCE_CATALOG_ROOT_DIGEST_MISMATCH,
        "source-catalog-duplicate-logical-identity": FixtureCaseDiagnostic.SOURCE_CATALOG_DUPLICATE_IDENTITY,
        "source-catalog-extra-member": FixtureCaseDiagnostic.SOURCE_CATALOG_EXTRA_MEMBER,
        "source-catalog-identity-drift": FixtureCaseDiagnostic.SOURCE_CATALOG_IDENTITY_DRIFT,
        "source-catalog-member-digest-mismatch": FixtureCaseDiagnostic.SOURCE_CATALOG_MEMBER_DIGEST_MISMATCH,
        "source-catalog-missing-member": FixtureCaseDiagnostic.SOURCE_CATALOG_MISSING_MEMBER,
        "source-catalog-path-escape": FixtureCaseDiagnostic.SOURCE_CATALOG_PATH_ESCAPE,
        "source-catalog-unknown-format": FixtureCaseDiagnostic.SOURCE_CATALOG_UNKNOWN_FORMAT,
    }


def test_source_catalog_producer_preserves_conflicting_immutable_fixture_bytes(tmp_path: Path) -> None:
    distribution = _fixtures()
    snapshot = distribution.case("source-catalog-snapshot")
    catalogs, reference = _write_positive_case(distribution, snapshot, tmp_path / "catalogs")
    catalog_path = catalogs.root / reference.locator
    drift = distribution.case("source-catalog-identity-drift")
    conflicting_bytes = _member_bytes(distribution, drift, "catalog.json")
    catalog_path.write_bytes(conflicting_bytes)

    with pytest.raises(IntegrityError, match="root differs from its reference"):
        _write_positive_case(distribution, snapshot, tmp_path / "catalogs")
    assert catalog_path.read_bytes() == conflicting_bytes
