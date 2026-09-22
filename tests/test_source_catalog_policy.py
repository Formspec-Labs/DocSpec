"""Source-catalog policy contract: the generic builder accepts an injected policy and configuration shape,
streams each source's records and renditions exactly once while merging multiple sources globally, and retains
each row's normalization, selection and topic interpretations as evidence.

Covers stable repeated-value indices in diagnostics, separate row families selected from one source system,
malformed or mixed metadata values retained without aborting neighbors, explicit dispositions for missing
required metadata or renditions, rendition preference recording every offer, the Federal Register policy
requiring its current schema version, topic recovery only with evidence, and an observed crawl not claiming
source completeness.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.catalog_artifact import digests as catalog_digests
from docspec.adapters.catalog_artifact.builder import SourceCatalogBuilder, SourceCatalogBuildRequest
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.source_catalog import CatalogDisposition, SourceCatalogItem
from docspec.ports.source_catalog import CatalogPolicyInputs, CatalogPolicyWorkspace, SourceInputSelector
from tests.support.source_catalog import (
    _FEDERAL_REGISTER_SOURCE,
    _SHA_C,
    FakeSource,
    description,
    producer,
    record,
    renditions,
)
from tests.support.source_catalog_builds import (
    build,
    interpretation_result,
    normalization_fields,
)


def test_normalized_diagnostic_values_use_stable_repeated_value_indices() -> None:
    assert list(catalog_digests._indexed_values(["EPA", "DOE"])) == [
        (0, "EPA"),
        (1, "DOE"),
    ]
    assert list(catalog_digests._indexed_values([])) == [(0, [])]
    assert list(catalog_digests._indexed_values("EPA")) == [(0, "EPA")]


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
        def universe_inputs(self) -> tuple[SourceInputSelector, ...]:
            return FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).universe_inputs

        def iter_items(
            self,
            inputs: CatalogPolicyInputs,
            workspace: CatalogPolicyWorkspace,
        ) -> Iterator[SourceCatalogItem]:
            for item in FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE).iter_items(inputs, workspace):
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
        def universe_inputs(self) -> tuple[SourceInputSelector, ...]:
            return self.delegate.universe_inputs

        def iter_items(
            self,
            inputs: CatalogPolicyInputs,
            workspace: CatalogPolicyWorkspace,
        ) -> Iterator[SourceCatalogItem]:
            assert [row.record["sourceRecordId"] for row in inputs.iter_lookup_rows(lookup_selector)] == [
                "environmental-protection-agency"
            ]
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
    item = next(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items)

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
    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)
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
    item = next(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items)

    assert [candidate.rendition_id for candidate in item.candidate_renditions] == [f"{identity}/body-html"]
    preference = interpretation_result(item, "rendition-preference")
    assert preference["orderedFamilyIds"] == (
        "full_text_xml_url",
        "body_html_url",
        "html_url",
        "pdf_url",
    )
    assert preference["selectedFamilyId"] == "body_html_url"
    assert [family["offeredRenditionIds"] for family in preference["families"]] == [
        (),
        (f"{identity}/body-html",),
        (f"{identity}/html",),
        (f"{identity}/pdf",),
    ]


@pytest.mark.parametrize("xml_url", ["https://publisher.example/native.xml", None])
def test_publisher_xml_is_selected_when_offered_with_html_as_an_absent_xml_alternative(tmp_path, xml_url):
    identity = "2026-00001"
    native = record(identity)
    native["record"]["full_text_xml_url"] = xml_url
    xml = {"sourceRecordId": identity, "renditionId": f"{identity}/body-xml", "sourceField": "full_text_xml_url",
           "locator": xml_url, "mediaType": "application/xml", "expectedSha256": None, "expectedByteSize": None}
    source = FakeSource(description(), (native,), (xml, *renditions(identity)))
    store, result = build(tmp_path, source)
    item = next(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items)
    selected, = item.candidate_renditions
    expected_id = f"{identity}/body-xml" if xml_url else f"{identity}/html"
    assert selected.rendition_id == expected_id
    assert selected.media_type == ("application/xml" if xml_url else "text/html")
    assert selected.locator == (xml_url or native["record"]["html_url"])
    preference = interpretation_result(item, "rendition-preference")
    assert preference["selectedFamilyId"] == ("full_text_xml_url" if xml_url else "html_url")
    assert preference["families"][-1]["offeredRenditionIds"] == (f"{identity}/pdf",)


def test_federal_register_policy_requires_current_schema_and_rejects_earlier_policy():
    policy = FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE)
    assert policy.universe_inputs[0].schema_version == "1.1" and policy.policy_version == "1.2.0"
    prior = policy.to_member() | {"policyVersion": "1.0.0"}
    with pytest.raises(ValueError, match="installed policy version"):
        FederalRegisterCatalogPolicy.from_member(prior)


def test_raw_agency_headings_are_evidence_not_identifiers():
    native = record("96-1584")["record"]
    heading = {"raw_name": "Food Additives Permitted for Direct Addition to Food for Human"}
    numbered = {"id": 145, "name": "Environmental Protection Agency"}
    native["agencies"] = [heading, numbered]
    normalized, fields, _ = FederalRegisterCatalogPolicy._normalization(native)
    assert normalized["agencies"] == [
        {"agencyId": "federal-register:145", "agencyName": "Environmental Protection Agency"}
    ]
    agencies = next(field for field in fields if field.normalized_field == "agencies")
    assert heading in agencies.unparseable_values
    assert native["agencies"] == [heading, numbered]


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
        SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items
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
    assert interpretation_result(observed_item, "topic-recovery")["observedTopicIds"] == ("air-quality",)


def test_accounts_for_an_observed_crawl_without_claiming_source_completeness(
    tmp_path: Path,
) -> None:
    observed = FakeSource(
        description(scope="observed-crawl"),
        (record("2026-00001"),),
        renditions("2026-00001"),
    )
    store, result = build(tmp_path / "observed", observed)
    snapshot = SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference)

    assert snapshot.summary.item_count == 1
    assert [item.source_item_id for item in snapshot.items] == ["2026-00001"]
