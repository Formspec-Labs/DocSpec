"""Source catalog publication and local transport fixtures.

``write_shared_source_catalog`` publishes exact current-format catalog items
under a fixed ``https://t.test/`` origin, and ``SharedFixtureContentFetcher``
serves that origin from an injected local reader.
"""
from __future__ import annotations
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from rulespec_artifacts import Producer
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.catalog_artifact.builder import SourceCatalogBuilder, SourceCatalogBuildRequest
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.identity import canonical_json_bytes, identity_digest, sha256_digest
from docspec.domain.references import SourceCatalogRef
from docspec.domain.source_catalog import (CatalogDisposition, CatalogNormalizationField, CatalogRenditionFamily, CatalogSelectionDecision, SourceCatalogCandidate, SourceCatalogItem, SourceCatalogSelection)
from docspec.ports.content_fetcher import FetchStream
from docspec.ports.source_catalog import CatalogPolicyInputs, CatalogPolicyWorkspace, SourceInputSelector, SourceNativeDescription

_FIXTURE_SOURCE_ORIGIN = "https://t.test/"
_FIXTURE_SCHEMA_DIGEST = "sha256:" + "f" * 64
_FIXTURE_SOURCE_SYSTEM = "urn:docspec:test:source-native"
_FIXTURE_SELECTOR = SourceInputSelector(
    _FIXTURE_SOURCE_SYSTEM,
    "1",
    "s",
    "i",
    "1.0",
)


def source_catalog_producer() -> Producer:
    """Return the fixed docspec source-catalog producer and verifier pin."""

    implementation = "git+https://example.test/docspec@" + "1" * 40
    return Producer(
        "docspec",
        implementation,
        "urn:docspec:verifier:source-catalog",
        "1.0.0",
        implementation,
    )



@dataclass(frozen=True, slots=True)
class _FixtureCatalogPolicy:
    """Minimal catalog policy whose universe is the one fixture selector.

    ``iter_items`` refuses a source-native payload whose keys are not exactly ``{"catalogItem"}``.
    """

    policy_id = "p"
    policy_version = "1"

    @property
    def universe_inputs(self) -> tuple[SourceInputSelector, ...]:
        return (_FIXTURE_SELECTOR,)

    @property
    def configuration(self) -> Mapping[str, object]:
        return {
            "universeInputs": [
                selector.to_dict() for selector in self.universe_inputs
            ]
        }

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

    def iter_items(
        self,
        inputs: CatalogPolicyInputs,
        workspace: CatalogPolicyWorkspace,
    ) -> Iterator[SourceCatalogItem]:
        del workspace
        for row in inputs.iter_universe_rows():
            payload = row.record["record"]
            if not isinstance(payload, Mapping) or set(payload) != {"catalogItem"}:
                raise ValueError("test source-native payload differs")
            yield SourceCatalogItem.from_dict(payload["catalogItem"])


@dataclass(slots=True)
class _FixtureSource:
    """Source-native fixture over prepared records, with no renditions."""

    metadata: SourceNativeDescription
    records: tuple[Mapping[str, object], ...]

    def describe(self) -> SourceNativeDescription:
        return self.metadata

    def iter_records(self) -> Iterator[Mapping[str, object]]:
        yield from self.records

    def iter_renditions(self) -> Iterator[Mapping[str, object]]:
        return
        yield


def shared_source_record(item: SourceItem) -> dict[str, object]:
    """Map one processing fixture into the current normative catalog shape."""

    disposition = {
        SourceItemState.ACTIVE: "selected",
        SourceItemState.DELETED: "deleted",
        SourceItemState.EXCLUDED: "excluded",
    }[item.state]
    selected_disposition = CatalogDisposition(disposition)
    reason_code = None if disposition == "selected" else f"test.{disposition}"
    reason = None if disposition == "selected" else f"Test item is {disposition}."
    candidates: list[SourceCatalogCandidate] = []
    for candidate in item.candidates:
        locator = candidate.locator
        if not locator.startswith(("http://", "https://")):
            locator = _FIXTURE_SOURCE_ORIGIN + quote(locator, safe="/")
        candidates.append(
            SourceCatalogCandidate(
                candidate.candidate_id,
                candidate.media_type,
                "source-url",
                locator,
                candidate.expected_digest,
                candidate.expected_size,
            )
        )
    policy = _FixtureCatalogPolicy()
    pin = {
        "policyId": policy.policy_id,
        "policyVersion": policy.policy_version,
        "policyDigest": policy.policy_digest,
        "inputScopeIds": [_FIXTURE_SELECTOR.scope_id],
    }
    candidate_ids = [candidate.rendition_id for candidate in candidates]
    decision = CatalogSelectionDecision(
        "s",
        disposition == "selected",
        None if disposition == "selected" else selected_disposition,
        reason_code,
        reason,
    )
    normalized = {
        "title": "T",
        "agencies": [],
        "documentType": None,
        "publicationDate": None,
        "lastUpdatedDate": None,
        "docketIds": [],
        "regulationIdentifierNumbers": [],
        "commentCloseDate": None,
        "language": None,
        "sourceUrl": None,
    }
    return SourceCatalogItem(
        source_item_id=item.item_id,
        document_id=item.item_id,
        source_issued_version=item.version,
        source_native_facts=(
            {
                "scopeId": _FIXTURE_SELECTOR.scope_id,
                "schemaName": _FIXTURE_SELECTOR.schema_name,
                "schemaVersion": _FIXTURE_SELECTOR.schema_version,
                "schemaDigest": _FIXTURE_SCHEMA_DIGEST,
                "fields": {"metadata": item.metadata},
            },
        ),
        normalized_metadata=normalized,
        source_observed_topics=(),
        source_observations=(),
        interpretations=(
            {
                "interpretationKind": "exact-join",
                **pin,
                "result": {"joins": []},
            },
            {
                "interpretationKind": "normalization",
                **pin,
                "result": {
                    "fields": [
                        CatalogNormalizationField(
                            "title",
                            ("i",),
                            "source",
                            "normalized",
                            "T",
                        ).to_dict()
                    ]
                },
            },
            {
                "interpretationKind": "rendition-preference",
                **pin,
                "result": {
                    "orderedFamilyIds": ["f"],
                    "families": [CatalogRenditionFamily("f", tuple(candidate_ids)).to_dict()],
                    "selectedFamilyId": "f" if candidates else None,
                    "selectedRenditionIds": candidate_ids,
                },
            },
            {
                "interpretationKind": "sampling",
                **pin,
                "result": {
                    "frameAdmitted": True,
                    "partition": "p",
                    "stratum": ["s"],
                    "orderHash": None,
                    "rank": None,
                    "stratumSize": None,
                    "allocationMethod": "all",
                    "limit": None,
                    "drawn": True,
                },
            },
            {
                "interpretationKind": "selection",
                **pin,
                "result": {
                    "decisions": [decision.to_dict()],
                    "finalDisposition": disposition,
                    "reasonCode": reason_code,
                    "reason": reason,
                },
            },
            {
                "interpretationKind": "topic-recovery",
                **pin,
                "result": {
                    "sourceField": "t",
                    "outcome": "not-recovered",
                    "evidenceDigest": None,
                    "observedTopicIds": [],
                },
            },
        ),
        candidate_renditions=tuple(candidates),
        selection=SourceCatalogSelection(selected_disposition, reason_code, reason),
    ).to_dict()


def write_shared_source_catalog(
    root: Path,
    items: tuple[SourceItem, ...],
    *,
    name: str = "catalog",
) -> SourceCatalogRef:
    """Publish a small exact current-format source catalog for tests."""

    catalog_items = tuple(shared_source_record(item) for item in items)
    records = tuple(
        {
            "sourceRecordId": item.item_id,
            "scopeId": _FIXTURE_SELECTOR.scope_id,
            "schemaName": _FIXTURE_SELECTOR.schema_name,
            "schemaVersion": _FIXTURE_SELECTOR.schema_version,
            "schemaDigest": _FIXTURE_SCHEMA_DIGEST,
            "record": {"catalogItem": catalog_item},
            "fieldDiagnostics": [],
        }
        for item, catalog_item in zip(items, catalog_items, strict=True)
    )
    state_digest = sha256_digest(canonical_json_bytes(list(records)))
    source = _FixtureSource(
        SourceNativeDescription(
            logical_id="urn:docspec:test:source-native:" + state_digest.removeprefix("sha256:"),
            artifact_digest=state_digest,
            source_system_id=_FIXTURE_SELECTOR.source_system_id,
            source_system_version=_FIXTURE_SELECTOR.source_system_version,
            source_state_scope="complete-snapshot",
            source_state_digest=state_digest,
            source_native_schema_set_digest=_FIXTURE_SCHEMA_DIGEST,
        ),
        records,
    )
    result = SourceCatalogBuilder(
        store=LocalSourceCatalogStore(root),
        policy=_FixtureCatalogPolicy(),
        request=SourceCatalogBuildRequest(f"urn:docspec:test:catalog:{name}", source_catalog_producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    return result.reference


def source_catalog_reader(root: Path) -> SourceCatalogArtifactReader:
    """Open the shared fixture catalog read-only through the artifact reader."""

    return SourceCatalogArtifactReader(
        LocalSourceCatalogStore(root, create=False),
        producer=source_catalog_producer(),
    )


class SharedFixtureContentFetcher:
    """Resolve the shared test HTTPS namespace through an injected local reader.

    A locator outside ``https://t.test/`` is refused, and every returned stream
    is relabelled with this fetcher's downloader identity and configuration digest.
    """

    downloader_id = "docspec.test.shared-fixture-content-fetcher.v1"

    def __init__(self, root: Path) -> None:
        self._local = LocalFileContentFetcher(root)
        self.configuration_digest = identity_digest({
            "implementationId": self.downloader_id,
            "sourceOrigin": _FIXTURE_SOURCE_ORIGIN,
            "localConfigurationDigest": self._local.configuration_digest,
        })

    def fetch(self, candidate: CandidateFile, **kwargs):  # type: ignore[no-untyped-def]
        parsed = urlsplit(candidate.locator)
        if parsed.scheme != "https" or parsed.netloc != "t.test":
            raise ValueError("shared fixture candidate is outside the test source namespace")
        local = replace(candidate, locator=unquote(parsed.path.lstrip("/")))
        result = self._local.fetch(local, **kwargs)
        return FetchStream(
            replace(
                result.metadata,
                downloader_id=self.downloader_id,
                downloader_configuration_digest=self.configuration_digest,
                transport_version=candidate.transport_version,
            ),
            result.chunks,
            result.close_callback,
        )


