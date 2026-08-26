from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from rulespec_artifacts import ArtifactPin, Producer

import docspec.adapters.source_catalog_artifact as source_catalog_artifact
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.adapters.source_catalog_artifact import (
    SourceCatalogArtifactReader,
    SourceCatalogBuildRequest,
    SourceCatalogBuilder,
    requested_universe_set_digest,
    selected_source_set_digest,
)
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.domain.source_catalog import CatalogDisposition, SourceCatalogItem
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, sha256_digest
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import (
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    SourceInputSelector,
    SourceNativeDescription,
)
from docspec.entrypoint import main

_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64
_FEDERAL_REGISTER_SOURCE = "https://www.federalregister.gov/api/v1"


def producer() -> Producer:
    implementation = "git+https://example.test/docspec@" + "1" * 40
    return Producer(
        "docspec",
        implementation,
        "urn:docspec:verifier:source-catalog",
        "1.0.0",
        implementation,
    )


def description(*, scope: str = "complete-snapshot") -> SourceNativeDescription:
    return SourceNativeDescription(
        logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "a" * 64,
        artifact_digest=_SHA_A,
        source_system_id=_FEDERAL_REGISTER_SOURCE,
        source_system_version="v1",
        source_state_scope=scope,
        source_state_digest=_SHA_B,
        source_native_schema_set_digest=_SHA_C,
    )


def record(identity: str, *, malformed_rin: bool = False, agencies: bool = True) -> dict[str, Any]:
    return {
        "sourceRecordId": identity,
        "scopeId": "federal-register-documents",
        "schemaName": "federal-register-document",
        "schemaVersion": "1.0",
        "schemaDigest": _SHA_C,
        "record": {
            "document_number": identity,
            "title": f"Federal Register {identity}",
            "type": "Rule",
            "publication_date": "2026-08-24",
            "agencies": (
                [{"slug": "environmental-protection-agency", "name": "Environmental Protection Agency"}]
                if agencies
                else []
            ),
            "html_url": f"https://www.federalregister.gov/d/{identity}",
            "pdf_url": f"https://example.test/{identity}.pdf",
            "docket_ids": ["EPA-HQ-2026-0001"],
            "regulation_id_numbers": ["not a rin" if malformed_rin else "2060-AV12"],
            "topics": [{"slug": "air-quality", "name": "Air quality"}],
        },
        "fieldDiagnostics": [],
    }


def renditions(identity: str) -> tuple[dict[str, Any], ...]:
    return (
        {
            "sourceRecordId": identity,
            "renditionId": f"{identity}/html",
            "sourceField": "html_url",
            "locator": f"https://www.federalregister.gov/d/{identity}",
            "mediaType": "text/html",
            "expectedSha256": None,
            "expectedByteSize": None,
        },
        {
            "sourceRecordId": identity,
            "renditionId": f"{identity}/pdf",
            "sourceField": "pdf_url",
            "locator": f"https://example.test/{identity}.pdf",
            "mediaType": "application/pdf",
            "expectedSha256": None,
            "expectedByteSize": None,
        },
    )


@dataclass
class FakeSource:
    metadata: SourceNativeDescription
    records: tuple[Mapping[str, Any], ...]
    renditions: tuple[Mapping[str, Any], ...]

    def describe(self) -> SourceNativeDescription:
        return self.metadata

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        yield from self.records

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
        yield from self.renditions


def build(root: Path, source: FakeSource):
    store = LocalSourceCatalogStore(root)
    result = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    return store, result


def interpretation_result(item: SourceCatalogItem, kind: str) -> Mapping[str, Any]:
    interpretation = next(
        value for value in item.interpretations if value["interpretationKind"] == kind
    )
    result = interpretation["result"]
    assert isinstance(result, Mapping)
    return result


def normalization_fields(item: SourceCatalogItem) -> dict[str, Mapping[str, Any]]:
    fields = interpretation_result(item, "normalization")["fields"]
    assert isinstance(fields, tuple)
    return {field["normalizedField"]: field for field in fields}


def assert_no_published_catalog(root: Path) -> None:
    assert not [path for path in root.iterdir() if path.name != ".staging"]
    staging = root / ".staging"
    if staging.exists():
        assert not tuple(staging.iterdir())


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
    item = next(snapshot.items)
    assert item.source_item_id == "2026-00001"
    assert item.disposition is CatalogDisposition.SELECTED
    assert item.normalized_metadata["regulationIdentifierNumbers"] == ("2060-AV12",)
    assert item.source_native_facts[0]["fields"]["document_number"] == "2026-00001"
    assert [value.media_type for value in item.candidate_renditions] == ["text/html"]
    expected_policy_digest = FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).policy_digest
    assert all(value["policyDigest"] == expected_policy_digest for value in item.interpretations)
    assert [value["interpretationKind"] for value in item.interpretations] == [
        "normalization",
        "rendition-preference",
        "selection",
        "topic-recovery",
    ]
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

    processing_snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(
        result.reference
    )
    assert processing_snapshot.summary.disposition_counts == {
        "selected": 1,
        "excluded": 0,
        "deleted": 0,
        "unavailable": 0,
        "failed": 0,
    }
    assert [value.to_processing_item().item_id for value in processing_snapshot.items] == [
        "2026-00001"
    ]


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


def test_generic_builder_accepts_a_second_injected_policy_configuration_shape(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class AlternatePolicy:
        policy_id = "urn:docspec:test:catalog-policy:alternate"
        policy_version = "2.0.0"

        @property
        def configuration(self) -> Mapping[str, Any]:
            return {"mode": "alternate", "settings": {"preserveSourceOrder": True}}

        @property
        def policy_digest(self) -> str:
            return sha256_digest(
                canonical_json_bytes(
                    {
                        "format": "docspec-catalog-policy",
                        "formatVersion": "1.0",
                        "policyId": self.policy_id,
                        "policyVersion": self.policy_version,
                        "configuration": dict(self.configuration),
                    }
                )
            )

        @property
        def universe_input(self) -> SourceInputSelector:
            return FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).universe_input

        def iter_items(
            self,
            inputs: CatalogPolicyInputs,
            workspace: CatalogPolicyWorkspace,
        ) -> Iterator[SourceCatalogItem]:
            for item in FederalRegisterCatalogPolicy(
                _FEDERAL_REGISTER_SOURCE
            ).iter_items(inputs, workspace):
                value = item.to_dict()
                for interpretation in value["interpretations"]:
                    interpretation["policyId"] = self.policy_id
                    interpretation["policyVersion"] = self.policy_version
                    interpretation["policyDigest"] = self.policy_digest
                yield SourceCatalogItem.from_dict(value)

    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    store = LocalSourceCatalogStore(tmp_path)
    result = SourceCatalogBuilder(
        store=store,
        policy=AlternatePolicy(),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:alternate", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))

    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)
    assert snapshot.summary.item_count == 1
    assert next(snapshot.items).source_item_id == "2026-00001"


def test_immutable_store_refuses_replacement(tmp_path: Path) -> None:
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    build(tmp_path, source)

    with pytest.raises(IntegrityError, match="already exists"):
        build(tmp_path, source)


def test_multi_source_rows_are_streamed_once_and_globally_merged(tmp_path: Path) -> None:
    class OnePassSource(FakeSource):
        records_opened = 0
        renditions_opened = 0

        def iter_records(self) -> Iterator[Mapping[str, Any]]:
            self.records_opened += 1
            assert self.records_opened == 1
            yield from self.records

        def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
            self.renditions_opened += 1
            assert self.renditions_opened == 1
            yield from self.renditions

    first = OnePassSource(
        description(),
        (record("2026-00001"), record("2026-00003")),
        (*renditions("2026-00001"), *renditions("2026-00003")),
    )
    second = OnePassSource(
        replace(
            description(),
            logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "d" * 64,
            artifact_digest="sha256:" + "d" * 64,
            source_state_digest="sha256:" + "e" * 64,
        ),
        (record("2026-00002"), record("2026-00004")),
        (*renditions("2026-00002"), *renditions("2026-00004")),
    )
    store = LocalSourceCatalogStore(tmp_path)
    result = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((first, second))

    items = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items
    assert [item.source_item_id for item in items] == [
        "2026-00001",
        "2026-00002",
        "2026-00003",
        "2026-00004",
    ]
    assert (first.records_opened, first.renditions_opened) == (1, 1)
    assert (second.records_opened, second.renditions_opened) == (1, 1)


def test_one_pass_facade_selects_separate_row_families_from_the_same_source_system(
    tmp_path: Path,
) -> None:
    class OnePassSource(FakeSource):
        records_opened = 0
        renditions_opened = 0

        def iter_records(self) -> Iterator[Mapping[str, Any]]:
            self.records_opened += 1
            assert self.records_opened == 1
            yield from self.records

        def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
            self.renditions_opened += 1
            assert self.renditions_opened == 1
            yield from self.renditions

    lookup_selector = SourceInputSelector(
        _FEDERAL_REGISTER_SOURCE,
        "v1",
        "federal-register-agencies",
        "federal-register-agency",
        "1.0",
    )
    lookup_record = {
        "sourceRecordId": "environmental-protection-agency",
        "scopeId": lookup_selector.scope_id,
        "schemaName": lookup_selector.schema_name,
        "schemaVersion": lookup_selector.schema_version,
        "schemaDigest": _SHA_C,
        "record": {"slug": "environmental-protection-agency"},
        "fieldDiagnostics": [],
    }
    universe_source = OnePassSource(
        description(),
        (record("2026-00001"),),
        renditions("2026-00001"),
    )
    lookup_source = OnePassSource(
        replace(
            description(),
            logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "d" * 64,
            artifact_digest="sha256:" + "d" * 64,
            source_state_digest="sha256:" + "e" * 64,
        ),
        (lookup_record,),
        (),
    )

    @dataclass(frozen=True)
    class LookupPolicy:
        delegate: FederalRegisterCatalogPolicy

        @property
        def policy_id(self) -> str:
            return self.delegate.policy_id

        @property
        def policy_version(self) -> str:
            return self.delegate.policy_version

        @property
        def configuration(self) -> Mapping[str, Any]:
            return self.delegate.configuration

        @property
        def universe_input(self) -> SourceInputSelector:
            return self.delegate.universe_input

        def iter_items(
            self,
            inputs: CatalogPolicyInputs,
            workspace: CatalogPolicyWorkspace,
        ) -> Iterator[SourceCatalogItem]:
            assert [
                row.record["sourceRecordId"]
                for row in inputs.iter_lookup_rows(lookup_selector)
            ] == ["environmental-protection-agency"]
            yield from self.delegate.iter_items(inputs, workspace)

    store = LocalSourceCatalogStore(tmp_path)
    result = SourceCatalogBuilder(
        store=store,
        policy=LookupPolicy(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE)),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((universe_source, lookup_source))

    assert result.summary.item_count == 1
    assert (universe_source.records_opened, universe_source.renditions_opened) == (1, 1)
    assert (lookup_source.records_opened, lookup_source.renditions_opened) == (1, 1)


def test_duplicate_source_item_across_inputs_cannot_publish(tmp_path: Path) -> None:
    first = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    second = FakeSource(
        replace(
            description(),
            logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "d" * 64,
            artifact_digest="sha256:" + "d" * 64,
        ),
        (record("2026-00001"),),
        renditions("2026-00001"),
    )
    store = LocalSourceCatalogStore(tmp_path)
    builder = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    )

    with pytest.raises(IntegrityError, match="repeat a sourceRecordId"):
        builder.build((first, second))

    assert_no_published_catalog(tmp_path)


def test_policy_must_account_for_every_universe_row_before_publication(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class DroppingPolicy:
        delegate: FederalRegisterCatalogPolicy

        @property
        def policy_id(self) -> str:
            return self.delegate.policy_id

        @property
        def policy_version(self) -> str:
            return self.delegate.policy_version

        @property
        def configuration(self) -> Mapping[str, Any]:
            return self.delegate.configuration

        @property
        def universe_input(self) -> SourceInputSelector:
            return self.delegate.universe_input

        def iter_items(
            self,
            inputs: CatalogPolicyInputs,
            workspace: CatalogPolicyWorkspace,
        ) -> Iterator[SourceCatalogItem]:
            for index, item in enumerate(self.delegate.iter_items(inputs, workspace)):
                if index != 1:
                    yield item

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002")),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    builder = SourceCatalogBuilder(
        store=LocalSourceCatalogStore(tmp_path),
        policy=DroppingPolicy(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE)),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    )

    with pytest.raises(IntegrityError, match="complete universe"):
        builder.build((source,))

    assert_no_published_catalog(tmp_path)


def test_source_stream_failure_cannot_publish_a_partial_catalog(tmp_path: Path) -> None:
    class FailingSource(FakeSource):
        def iter_records(self) -> Iterator[Mapping[str, Any]]:
            yield self.records[0]
            raise RuntimeError("source stream failed")

    source = FailingSource(
        description(),
        (record("2026-00001"), record("2026-00002")),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )

    with pytest.raises(RuntimeError, match="source stream failed"):
        build(tmp_path, source)

    assert_no_published_catalog(tmp_path)


def test_catalog_row_limit_fails_before_publication(tmp_path: Path) -> None:
    oversized = record("2026-00001")
    oversized["record"]["title"] = "x" * source_catalog_artifact.MAX_CATALOG_ROW_BYTES
    source = FakeSource(description(), (oversized,), renditions("2026-00001"))

    with pytest.raises(LimitExceededError, match="row exceeds"):
        build(tmp_path, source)

    assert_no_published_catalog(tmp_path)


def test_concurrent_builders_publish_exactly_one_immutable_winner(tmp_path: Path) -> None:
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

    assert len(results) == 1
    assert len(errors) == 1
    store, result = results[0]
    summary = SourceCatalogArtifactReader(store, producer=producer()).verify_snapshot(
        result.reference
    )
    assert summary == result.summary


def test_producer_gate_recomputes_state_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_digest_function = source_catalog_artifact._catalog_state_digests
    calls = 0

    def wrong_initial_state(*args: Any, **kwargs: Any) -> tuple[str, str, str]:
        nonlocal calls
        calls += 1
        state, requested, selected = actual_digest_function(*args, **kwargs)
        if calls == 1:
            state = "sha256:" + "f" * 64
        return state, requested, selected

    monkeypatch.setattr(source_catalog_artifact, "_catalog_state_digests", wrong_initial_state)
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))

    with pytest.raises(IntegrityError, match="catalogStateDigest"):
        build(tmp_path, source)

    assert calls == 2
    assert_no_published_catalog(tmp_path)


def test_malformed_rin_is_retained_but_not_normalized_and_does_not_abort_neighbor(tmp_path: Path) -> None:
    source = FakeSource(
        description(),
        (record("2026-00001", malformed_rin=True), record("2026-00002")),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    store, result = build(tmp_path, source)
    items = tuple(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items)

    assert items[0].normalized_metadata["regulationIdentifierNumbers"] == ()
    assert items[0].source_native_facts[0]["fields"]["regulation_id_numbers"] == ("not a rin",)
    malformed = normalization_fields(items[0])["regulationIdentifierNumbers"]
    assert malformed["outcome"] == "unparseable"
    assert malformed["value"] == ()
    assert malformed["unparseableValues"] == ("not a rin",)
    assert items[1].normalized_metadata["regulationIdentifierNumbers"] == ("2060-AV12",)


def test_mixed_valid_and_malformed_metadata_values_are_reported_without_aborting(
    tmp_path: Path,
) -> None:
    mixed = record("2026-00001")
    mixed["record"]["docket_ids"] = ["EPA-HQ-2026-0001", 7, 7]
    source = FakeSource(description(), (mixed,), renditions("2026-00001"))
    store, result = build(tmp_path, source)
    item = next(
        SourceCatalogArtifactReader(store, producer=producer())
        .open_snapshot(result.reference)
        .items
    )

    field = normalization_fields(item)["docketIds"]
    assert item.disposition is CatalogDisposition.SELECTED
    assert item.normalized_metadata["docketIds"] == ("EPA-HQ-2026-0001",)
    assert field["outcome"] == "unparseable"
    assert field["value"] == ("EPA-HQ-2026-0001",)
    assert field["unparseableValues"] == (7,)


def test_missing_required_metadata_is_an_explicit_row_disposition(tmp_path: Path) -> None:
    source = FakeSource(description(), (record("2026-00001", agencies=False),), renditions("2026-00001"))
    store, result = build(tmp_path, source)
    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)
    item = next(snapshot.items)

    assert snapshot.summary.item_count == 1
    assert snapshot.summary.disposition_counts["failed"] == 1
    assert item.disposition is CatalogDisposition.FAILED
    assert item.selection.reason_code == "source.normalized-field-missing"
    decisions = interpretation_result(item, "selection")["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decisionId"] == "required-metadata"
    assert decisions[0]["outcome"] == "fail"
    assert decisions[0]["disposition"] == "failed"


def test_missing_rendition_is_unavailable_without_affecting_a_neighbor(tmp_path: Path) -> None:
    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002")),
        renditions("2026-00002"),
    )
    store, result = build(tmp_path, source)
    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(
        result.reference
    )
    unavailable, selected = tuple(snapshot.items)

    assert snapshot.summary.disposition_counts["unavailable"] == 1
    assert snapshot.summary.disposition_counts["selected"] == 1
    assert unavailable.disposition is CatalogDisposition.UNAVAILABLE
    assert unavailable.selection.reason_code == "source.no-candidate-rendition"
    assert unavailable.candidate_renditions == ()
    decisions = interpretation_result(unavailable, "selection")["decisions"]
    assert [decision["decisionId"] for decision in decisions] == [
        "required-metadata",
        "candidate-rendition",
    ]
    assert [decision["outcome"] for decision in decisions] == ["pass", "fail"]
    assert decisions[-1]["disposition"] == "unavailable"
    assert selected.disposition is CatalogDisposition.SELECTED


def test_rendition_preference_records_every_offer_and_selects_the_first_family(
    tmp_path: Path,
) -> None:
    identity = "2026-00001"
    body = {
        "sourceRecordId": identity,
        "renditionId": f"{identity}/body-html",
        "sourceField": "body_html_url",
        "locator": f"https://www.federalregister.gov/d/{identity}/body",
        "mediaType": "text/html",
        "expectedSha256": None,
        "expectedByteSize": None,
    }
    source_record = record(identity)
    source_record["record"]["body_html_url"] = body["locator"]
    source = FakeSource(description(), (source_record,), (body, *renditions(identity)))
    store, result = build(tmp_path, source)
    item = next(
        SourceCatalogArtifactReader(store, producer=producer())
        .open_snapshot(result.reference)
        .items
    )

    assert [candidate.rendition_id for candidate in item.candidate_renditions] == [
        f"{identity}/body-html"
    ]
    preference = interpretation_result(item, "rendition-preference")
    assert preference["orderedFamilyIds"] == (
        "body_html_url",
        "html_url",
        "pdf_url",
    )
    assert preference["selectedFamilyId"] == "body_html_url"
    assert [family["offeredRenditionIds"] for family in preference["families"]] == [
        (f"{identity}/body-html",),
        (f"{identity}/html",),
        (f"{identity}/pdf",),
    ]


def test_empty_topics_are_not_recovered_without_evidence_and_do_not_affect_a_neighbor(
    tmp_path: Path,
) -> None:
    empty = record("2026-00001")
    empty["record"]["topics"] = []
    source = FakeSource(
        description(),
        (empty, record("2026-00002")),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    store, result = build(tmp_path, source)
    empty_item, observed_item = tuple(
        SourceCatalogArtifactReader(store, producer=producer())
        .open_snapshot(result.reference)
        .items
    )

    assert empty_item.disposition is CatalogDisposition.SELECTED
    assert empty_item.source_observed_topics == ()
    assert interpretation_result(empty_item, "topic-recovery") == {
        "sourceField": "record.topics",
        "outcome": "not-recovered",
        "evidenceDigest": None,
        "observedTopicIds": (),
    }
    assert observed_item.disposition is CatalogDisposition.SELECTED
    assert interpretation_result(observed_item, "topic-recovery")["outcome"] == "observed"
    assert interpretation_result(observed_item, "topic-recovery")["observedTopicIds"] == (
        "air-quality",
    )


def test_accounts_for_an_observed_crawl_without_claiming_source_completeness(
    tmp_path: Path,
) -> None:
    observed = FakeSource(
        description(scope="observed-crawl"),
        (record("2026-00001"),),
        renditions("2026-00001"),
    )
    store, result = build(tmp_path / "observed", observed)
    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(
        result.reference
    )

    assert snapshot.summary.item_count == 1
    assert [item.source_item_id for item in snapshot.items] == ["2026-00001"]


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
    item_path = artifact_root / "records/source-items.jsonl"
    item_path.write_bytes(item_path.read_bytes() + b"{}\n")

    with pytest.raises(IntegrityError, match="source catalog artifact is invalid"):
        SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)


def install_fake_source_native(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeReader:
        def __init__(
            self,
            source,
            *,
            profile: object,
            accepted_verifier_implementation_ids: frozenset[str],
            expected_pin: ArtifactPin | None = None,
        ) -> None:
            assert source is not None
            assert profile is fake_profile
            assert expected_pin is None
            assert accepted_verifier_implementation_ids == frozenset({"urn:test:source-verifier:sha256:" + "9" * 64})
            self.pin = ArtifactPin(description().logical_id, description().artifact_digest)
            self.source_state_scope = description().source_state_scope
            self.source_system_id = description().source_system_id
            self.source_system_version = description().source_system_version
            self.source_state_digest = description().source_state_digest
            self.source_native_schema_set_digest = description().source_native_schema_set_digest

        def iter_records(self):
            yield record("2026-00001")

        def iter_renditions(self):
            yield from renditions("2026-00001")

    fake_profile = object()
    package_name = "spicy_" + "regs"
    module_name = package_name + ".source_native"
    profiles_module_name = package_name + ".source_native_profiles"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    module = ModuleType(module_name)
    profiles_module = ModuleType(profiles_module_name)
    module.SourceNativeReleaseReader = FakeReader  # type: ignore[attr-defined]
    profiles_module.FEDERAL_REGISTER_PROFILE = fake_profile  # type: ignore[attr-defined]
    profiles_module.REGULATIONS_GOV_DOCUMENT_PROFILE = object()  # type: ignore[attr-defined]
    profiles_module.REGULATIONS_GOV_DOCKET_PROFILE = object()  # type: ignore[attr-defined]
    package.source_native = module  # type: ignore[attr-defined]
    package.source_native_profiles = profiles_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setitem(sys.modules, profiles_module_name, profiles_module)


def source_catalog_build_arguments(
    tmp_path: Path,
    *,
    destination: Path,
    receipt_path: Path,
) -> list[str]:
    source_root = tmp_path / "source-native"
    source_root.mkdir(exist_ok=True)
    policy_path = tmp_path / "catalog-policy.json"
    policy_path.write_bytes(
        canonical_json_file_bytes(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).to_member())
    )
    implementation_id = "git+https://example.test/docspec@" + "1" * 40
    return [
        "source-catalog",
        "build",
        "--source-native",
        str(source_root),
        "--source-native-artifact-digest",
        _SHA_A,
        "--source-native-profile",
        "federal-register",
        "--accepted-source-verifier-implementation-id",
        "urn:test:source-verifier:sha256:" + "9" * 64,
        "--catalog-policy",
        str(policy_path),
        "--implementation-id",
        implementation_id,
        "--verifier-implementation-id",
        implementation_id,
        "--destination",
        str(destination),
        "--receipt",
        str(receipt_path),
    ]


def test_cli_composes_the_optional_source_adapter_and_emits_a_verifiable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    receipt_path = tmp_path / "catalog-build-receipt.json"
    arguments = source_catalog_build_arguments(
        tmp_path,
        destination=destination,
        receipt_path=receipt_path,
    )
    implementation_id = "git+https://example.test/docspec@" + "1" * 40

    assert main(arguments) == 0
    output = json.loads(capfd.readouterr().out)
    assert output["verdict"] == "pass"
    assert output["itemCount"] == 1
    assert output["catalog"] == json.loads(receipt_path.read_text())["catalog"]

    reference_path = tmp_path / "source-catalog-ref.json"
    reference_path.write_bytes(canonical_json_file_bytes(output["catalog"]))
    assert (
        main(
            [
                "source-catalog",
                "verify",
                "--root",
                str(destination),
                "--reference",
                str(reference_path),
                "--implementation-id",
                implementation_id,
                "--verifier-implementation-id",
                implementation_id,
            ]
        )
        == 0
    )
    verification = json.loads(capfd.readouterr().out)
    assert verification["logicalId"] == output["catalog"]["catalogId"]
    assert verification["itemMemberPath"] == "records/source-items.jsonl"


def test_cli_receipt_write_failure_leaves_no_published_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    import docspec.source_catalog_cli as source_catalog_cli

    install_fake_source_native(monkeypatch)
    destination = tmp_path / "catalog-store"
    receipt_path = tmp_path / "catalog-build-receipt.json"
    arguments = source_catalog_build_arguments(
        tmp_path,
        destination=destination,
        receipt_path=receipt_path,
    )

    def fail_receipt_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected receipt write failure")

    monkeypatch.setattr(source_catalog_cli, "_write_new", fail_receipt_write)

    assert main(arguments) == 2
    assert "injected receipt write failure" in capfd.readouterr().err
    assert not destination.exists()
    failure = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert failure["format"] == "docspec-operation-failure-receipt"
    assert failure["operation"] == "source-catalog.build"
    assert failure["verdict"] == "failed"
    assert not tuple(tmp_path.glob(".catalog-store.*.staging"))


@pytest.mark.parametrize("shared_output", ["destination", "receipt"])
def test_cli_concurrent_publishers_leave_one_artifact_and_one_success_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_output: str,
) -> None:
    import docspec.source_catalog_cli as source_catalog_cli

    install_fake_source_native(monkeypatch)
    monkeypatch.setattr(source_catalog_cli, "_emit", lambda *_args, **_kwargs: None)
    destinations = (
        (tmp_path / "catalog-store", tmp_path / "catalog-store")
        if shared_output == "destination"
        else (tmp_path / "catalog-store-a", tmp_path / "catalog-store-b")
    )
    receipt_paths = (
        (tmp_path / "receipt-a.json", tmp_path / "receipt-b.json")
        if shared_output == "destination"
        else (tmp_path / "catalog-build-receipt.json",) * 2
    )
    argument_sets = tuple(
        source_catalog_build_arguments(
            tmp_path,
            destination=destination,
            receipt_path=receipt_path,
        )
        for destination, receipt_path in zip(destinations, receipt_paths, strict=True)
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(main, argument_sets))

    assert sorted(results) == [0, 2]
    existing_destinations = {path for path in destinations if path.exists()}
    assert len(existing_destinations) == 1
    receipts = [json.loads(path.read_text()) for path in set(receipt_paths) if path.exists()]
    successful_receipts = [value for value in receipts if value["verdict"] == "pass"]
    assert len(successful_receipts) == 1
    receipt = successful_receipts[0]
    assert receipt["verdict"] == "pass"
    published_destination = next(iter(existing_destinations))
    assert receipt["destination"] == published_destination.resolve().as_posix()
    reference = SourceCatalogRef.from_dict(receipt["catalog"])
    summary = SourceCatalogArtifactReader(
        LocalSourceCatalogStore(published_destination, create=False),
        producer=producer(),
    ).verify_snapshot(reference)
    assert summary.item_count == 1
    assert not tuple(tmp_path.glob(".catalog-store*.staging"))
