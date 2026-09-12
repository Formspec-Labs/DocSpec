"""Exercise the installed runtime using the copied offline source fixture.

This runs in an isolated wheel environment, imports no test helpers, and writes
no caller-side plan or run-request files.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import runpy
import sys

from rulespec_artifacts import Producer

from docspec.adapters.storage import LocalJsonControlRepository
from docspec.domain.execution import ExecutionLimits
from docspec.domain.identity import identity_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.errors import IntegrityError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run
from docspec.source_catalog import (
    FederalRegisterCatalogPolicy,
    LocalSourceCatalogStore,
    SourceCatalogBuilder,
    SourceCatalogBuildRequest,
    SqliteCatalogPolicyWorkspace,
)
from docspec.workspace import LocalWorkspace


def main() -> None:
    example = runpy.run_path(str(Path.cwd() / "offline_demo.py"))
    source = example["ExampleSource"]()
    implementation_id = "urn:docspec:installed-runtime-test:" + sys.argv[1]
    source_producer = Producer(
        "docspec", implementation_id, "urn:docspec:verifier:source-catalog", "1.0.0", implementation_id,
    )
    release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
    workspace = LocalWorkspace(Path.cwd() / "dataset")
    store = LocalSourceCatalogStore(workspace.roots["sourceCatalog"])
    catalog = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(example["SOURCE_SYSTEM"]),
        request=SourceCatalogBuildRequest("urn:docspec:installed-runtime-test:catalog", source_producer),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    retry, accepted = RetryPolicy(), AcceptedFailurePolicy()
    class CountingProcessor(ContentStatisticsProcessor):
        calls = 0

        def process(self, *args, **kwargs):
            self.calls += 1
            return super().process(*args, **kwargs)

    processor = CountingProcessor(retry_policy=retry)
    plan = ProcessingPlan.create(
        source_catalog=catalog.reference,
        base_release=None,
        profiles=ProfileRegistry.builtin().local_profiles(),
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy(
            (DefaultExtractorRegistry.extractor_id,), DefaultSegmenterRegistry.segmenter_id,
            (processor.description.processor_id,),
        ),
        processors=ProcessorSet((processor.description,)),
        partition_count=2,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )

    class CountingFetcher(example["ExampleFetcher"]):
        calls = 0

        def fetch(self, candidate, **kwargs):
            self.calls += 1
            return super().fetch(candidate, **kwargs)

    fetcher = CountingFetcher(source)
    settings = dict(
        retry_policy=retry,
        accepted_failure_policy=accepted,
        source_catalog_producer=source_producer,
        document_release_producer=release_producer,
        execution_limits=ExecutionLimits(
            worker_count=1, max_concurrency_per_worker=1, max_in_flight=1,
            max_scratch_bytes_per_worker=4 * 1024**3, max_network_bytes_per_task=8 * 1024**3,
            request_rate_limit_per_second=100, max_provider_concurrency=4,
            max_task_attempts=1, retry_initial_delay_milliseconds=0, retry_max_delay_milliseconds=0,
        ),
        deadline_epoch_seconds=4_000_000_000,
        completed_at=example["COMPLETED_AT"],
        content_fetcher=fetcher,
        processors={processor.description.processor_id: processor},
    )
    prepared = prepare_local_run(plan, workspace, **settings)
    first = prepared.run()
    assert fetcher.calls == 1
    assert processor.calls == 1
    recovered = prepare_local_run(plan, workspace, handoff_ref=prepared.handoff_ref, **settings)
    assert recovered.run() == first
    assert fetcher.calls == 1
    assert processor.calls == 1

    # Unified result inspection remains separate work. Inspect real retained
    # output through the existing public storage adapter in this qualification.
    run = RunReceipt.from_dict(LocalJsonControlRepository(workspace.roots["controlRepository"]).load(first))
    counts = {layer.layer_kind: layer.record_count for layer in run.staged_layers}
    assert run.selected_item_count == 1
    assert counts["files"] == counts["representations"] == counts["segments"] == 1
    assert counts["failures"] == 0

    original_digest = fetcher.configuration_digest
    fetcher.configuration_digest = identity_digest({"differentConfiguration": True})
    try:
        prepare_local_run(plan, workspace, handoff_ref=prepared.handoff_ref, **settings)
    except IntegrityError:
        pass
    else:
        raise AssertionError("changed fetcher configuration reused a saved handoff")
    fetcher.configuration_digest = original_digest
    changed_workspace = LocalWorkspace(
        workspace.root, overrides={"reconciliation": workspace.root / "different-reconciliation"},
    )
    try:
        prepare_local_run(plan, changed_workspace, handoff_ref=prepared.handoff_ref, **settings)
    except IntegrityError:
        pass
    else:
        raise AssertionError("changed storage root reused a saved handoff")
    assert fetcher.calls == 1
    assert not (Path.cwd() / "plan.json").exists()
    assert not (Path.cwd() / "run-request.json").exists()
    print("installed runtime: captured once, recovered unchanged work, refused changed worker settings")


if __name__ == "__main__":
    main()
