"""Source-catalog build safety: bounded admission of policy-authored rows, failure at any stage before
publication leaves no catalog, and a killed build resumes from its committed workspace without recomputing
already-staged policy output.

Covers the distinct-join-identity limit, duplicate source item ids across inputs, dropped universe rows, a
source stream failure, row/rendition-count/aggregate-byte limits, a producer gate that recomputes the catalog
state before publication, resume publishing a byte-identical artifact while refusing a workspace staged by
another build, and publication retry reusing staged blobs without recomputation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from rulespec_artifacts import Producer

from docspec.adapters.catalog_artifact import derivation as catalog_derivation
from docspec.adapters.catalog_artifact import digests as catalog_digests
from docspec.adapters.catalog_artifact import rules as catalog_rules
from docspec.adapters.catalog_artifact.builder import SourceCatalogBuilder, SourceCatalogBuildRequest
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.adapters.source_catalog_store import staging as catalog_staging
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.references import SourceCatalogRef
from docspec.domain.source_catalog import SOURCE_CATALOG_MAX_JOIN_IDS, SourceCatalogItem
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import CatalogPolicyInputs, CatalogPolicyWorkspace, SourceInputSelector
from tests.support.catalog_interruptions import CountItems, KillAfter
from tests.support.source_catalog import _FEDERAL_REGISTER_SOURCE, FakeSource, description, producer, record, renditions
from tests.support.source_catalog_builds import (
    assert_no_published_catalog,
    build,
)


def test_join_coverage_refuses_unbounded_row_authored_identities(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class ExcessiveJoinPolicy:
        policy_id = "urn:docspec:test:catalog-policy:excessive-joins"
        policy_version = "1.0.0"

        @property
        def configuration(self) -> Mapping[str, Any]:
            return {"mode": "excessive-joins"}

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
                exact_join = next(
                    interpretation
                    for interpretation in value["interpretations"]
                    if interpretation["interpretationKind"] == "exact-join"
                )
                exact_join["result"]["joins"] = [
                    {
                        "joinId": f"join-{index:04d}",
                        "sourceField": "document_number",
                        "sourceValue": item.source_item_id,
                        "lookupScopeId": "federal-register-documents",
                        "outcome": "no-match",
                        "matchedSourceRecordId": None,
                    }
                    for index in range(SOURCE_CATALOG_MAX_JOIN_IDS + 1)
                ]
                yield SourceCatalogItem.from_dict(value)

    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    with pytest.raises(LimitExceededError, match="distinct-identity limit"):
        SourceCatalogBuilder(
            store=LocalSourceCatalogStore(tmp_path),
            policy=ExcessiveJoinPolicy(),
            request=SourceCatalogBuildRequest("urn:docspec:catalog:excessive-joins", producer()),
            workspace_factory=SqliteCatalogPolicyWorkspace,
        ).build((source,))

    assert_no_published_catalog(tmp_path)


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
        def universe_inputs(self) -> tuple[SourceInputSelector, ...]:
            return self.delegate.universe_inputs

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
    oversized["record"]["title"] = "x" * catalog_rules.MAX_CATALOG_ROW_BYTES
    source = FakeSource(description(), (oversized,), renditions("2026-00001"))

    with pytest.raises(LimitExceededError, match="row exceeds"):
        build(tmp_path, source)

    assert_no_published_catalog(tmp_path)


def test_source_rendition_count_limit_fails_before_eager_record_allocation(
    tmp_path: Path,
) -> None:
    class ExcessiveRenditionSource(FakeSource):
        def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
            for index in range(catalog_rules.MAX_SOURCE_RENDITIONS_PER_RECORD + 1):
                yield {
                    "sourceRecordId": "2026-00001",
                    "renditionId": f"2026-00001/{index:05d}",
                    "sourceField": "html_url",
                    "locator": f"https://example.test/{index}",
                    "mediaType": "text/html",
                    "expectedSha256": None,
                    "expectedByteSize": None,
                }

    source = ExcessiveRenditionSource(
        description(),
        (record("2026-00001"),),
        (),
    )
    with pytest.raises(LimitExceededError, match="rendition count"):
        build(tmp_path, source)

    assert_no_published_catalog(tmp_path)


def test_source_rendition_aggregate_byte_limit_fails_before_publication(
    tmp_path: Path,
) -> None:
    oversized = dict(renditions("2026-00001")[0])
    oversized["locator"] = "https://example.test/" + "x" * catalog_rules.MAX_SOURCE_RENDITION_BYTES_PER_RECORD
    source = FakeSource(description(), (record("2026-00001"),), (oversized,))

    with pytest.raises(LimitExceededError, match="rendition bytes"):
        build(tmp_path, source)

    assert_no_published_catalog(tmp_path)


def test_producer_gate_recomputes_state_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_derivation = catalog_derivation._derive_catalog
    calls = 0

    def wrong_initial_state(*args: Any, **kwargs: Any) -> catalog_digests._DerivedCatalog:
        nonlocal calls
        calls += 1
        derived = actual_derivation(*args, **kwargs)
        if calls == 1:
            derived = catalog_digests._DerivedCatalog(
                "sha256:" + "f" * 64,
                derived.requested_universe_set_digest,
                derived.selected_source_set_digest,
                derived.disposition_counts,
                derived.reason_counts,
                derived.diagnostics,
            )
        return derived

    monkeypatch.setattr(catalog_derivation, "_derive_catalog", wrong_initial_state)
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))

    with pytest.raises(IntegrityError, match="catalogStateDigest"):
        build(tmp_path, source)

    assert calls == 2
    assert_no_published_catalog(tmp_path)


def test_a_build_resumed_from_a_killed_workspace_publishes_the_identical_artifact(
    tmp_path: Path,
) -> None:
    """Resume acceptance rule, written before the feature existed: a build that dies mid-stream and resumes
    from its committed workspace publishes byte-for-byte the artifact a fresh build publishes, computes only
    the items after its last commit, and refuses a workspace staged under another build identity; two catalog-A
    builds died this way at 23 and 27.7 minutes with nothing readable left behind.
    """

    identities = tuple(f"2026-{index:05d}" for index in range(1, 8))

    def source() -> FakeSource:
        """A seven-record Federal Register fake source."""
        return FakeSource(
            description(),
            tuple(record(identity) for identity in identities),
            tuple(value for identity in identities for value in renditions(identity)),
        )

    def builder(
        root: Path,
        policy: object,
        workspace_factory: Any,
        *,
        build_producer: Producer | None = None,
    ) -> SourceCatalogBuilder:
        """A builder with a fixed request and two-item resume batches."""
        return SourceCatalogBuilder(
            store=LocalSourceCatalogStore(root),
            policy=policy,  # type: ignore[arg-type]
            request=SourceCatalogBuildRequest(
                "urn:docspec:catalog:federal-register", build_producer or producer()
            ),
            workspace_factory=workspace_factory,
            resume_batch_items=2,
        )

    def receipt_bytes(root: Path, reference: SourceCatalogRef) -> bytes:
        """The published catalog-build-receipt.json bytes for a catalog reference."""
        return (root / reference.digest.removeprefix("sha256:") / "catalog-build-receipt.json").read_bytes()

    fresh_root = tmp_path / "fresh"
    fresh = builder(
        fresh_root, FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE), SqliteCatalogPolicyWorkspace
    ).build((source(),))

    workspace_path = tmp_path / "resume" / "workspace.sqlite3"

    def durable() -> SqliteCatalogPolicyWorkspace:
        """Open the one on-disk workspace so the killed build's staging survives for resume."""
        return SqliteCatalogPolicyWorkspace(path=workspace_path)

    resumed_root = tmp_path / "resumed"
    killed = KillAfter(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE), yields=5)
    with pytest.raises(IntegrityError, match="injected kill mid-stream"):
        builder(resumed_root, killed, durable).build((source(),))
    assert killed.computed == 5
    assert workspace_path.exists()

    counted = CountItems(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE))
    resumed = builder(resumed_root, counted, durable).build((source(),))

    assert resumed.reference == fresh.reference
    # Items 1-4 were committed in two batches of two; item 5 was staged in a
    # batch the kill rolled back, so the resumed run recomputes 5, 6 and 7.
    assert counted.computed == 3
    assert receipt_bytes(resumed_root, resumed.reference) == receipt_bytes(fresh_root, fresh.reference)

    other_path = tmp_path / "other" / "workspace.sqlite3"
    other_root = tmp_path / "other-store"
    with pytest.raises(IntegrityError, match="injected kill mid-stream"):
        builder(
            other_root,
            KillAfter(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE), yields=5),
            lambda: SqliteCatalogPolicyWorkspace(path=other_path),
        ).build((source(),))
    other_implementation = "git+https://example.test/docspec@" + "2" * 40
    other_producer = Producer(
        "docspec",
        other_implementation,
        "urn:docspec:verifier:source-catalog",
        "1.0.0",
        other_implementation,
    )
    with pytest.raises(IntegrityError, match="staged by a different build"):
        builder(
            other_root,
            FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
            lambda: SqliteCatalogPolicyWorkspace(path=other_path),
            build_producer=other_producer,
        ).build((source(),))


def test_a_fully_staged_workspace_retries_publication_without_recomputing_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OnePassPolicy(CountItems):
        calls = 0

        def iter_items(self, inputs: object, workspace: object) -> Iterator[object]:
            self.calls += 1
            assert self.calls == 1, "fully staged policy output must not be recomputed"
            yield from super().iter_items(inputs, workspace)

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002")),
        renditions("2026-00002"),
    )
    root = tmp_path / "store"
    store = LocalSourceCatalogStore(root)
    workspace_path = tmp_path / "workspace.sqlite3"
    policy = OnePassPolicy(FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE))
    builder = SourceCatalogBuilder(
        store=store,
        policy=policy,  # type: ignore[arg-type]
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=lambda: SqliteCatalogPolicyWorkspace(path=workspace_path),
    )

    def fail_publication(*args: Any) -> None:
        raise IntegrityError("injected root publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(catalog_staging, "_publish_directory_no_replace_at", fail_publication)
        with pytest.raises(IntegrityError, match="injected root publication failure"):
            builder.build((source,))

    assert policy.computed == 2
    assert not [path for path in root.iterdir() if not path.name.startswith(".")]
    resumed = builder.build((source,))

    assert policy.calls == 1
    assert policy.computed == 2
    assert resumed.summary.item_count == 2
    assert resumed.summary.disposition_counts["selected"] == 1
    assert resumed.summary.disposition_counts["unavailable"] == 1
    assert resumed.summary.reason_counts == (
        {"disposition": "unavailable", "reasonCode": "source.no-candidate-rendition", "count": 1},
    )
    assert resumed.byte_measurements["payloadBytesWritten"] == 0
    assert resumed.byte_measurements["payloadBytesReused"] > 0
    assert SourceCatalogArtifactReader(store, producer=producer()).verify_snapshot(resumed.reference) == resumed.summary

    # A new workspace and publication root reuse the blobs and seal the same artifact.
    rebuilt = SourceCatalogBuilder(
        store=LocalSourceCatalogStore(tmp_path / "rebuilt", shared_blob_root=root / ".blobs"),
        policy=FederalRegisterCatalogPolicy(_FEDERAL_REGISTER_SOURCE),
        request=SourceCatalogBuildRequest("urn:docspec:catalog:federal-register", producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    assert resumed == rebuilt
