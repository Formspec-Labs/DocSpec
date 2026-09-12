"""Exercise the installed runtime using the copied offline source fixture.

This runs in an isolated wheel environment, imports no test helpers, and writes
no caller-side plan or run-request files.
"""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
import runpy
import sys

from rulespec_artifacts import Producer

from docspec.adapters.storage import LocalJsonControlRepository, LocalJsonlRecordStorage
from docspec.domain.execution import ExecutionLimits
from docspec.domain.identity import identity_digest
from docspec.domain.plans import ProcessingPlan, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.errors import IntegrityError, ProfileError
from docspec.processing.extraction import TextExtractor
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
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

    class InstalledExtractor(TextExtractor):
        extractor_id = "tests.installed-source-text/v1"
        configuration_digest = identity_digest({"mode": "retain-exact-source"})
        calls = 0

        def extract(self, *args):
            self.calls += 1
            return super().extract(*args)

    class InstalledSegmenter(ParagraphSegmenter):
        segmenter_id = "tests.installed-paragraphs/v1"
        policy_digest = identity_digest({"policy": "blank-line-paragraphs"})
        calls = 0

        def segment(self, *args):
            self.calls += 1
            return super().segment(*args)

    extractor, segmenter = InstalledExtractor(), InstalledSegmenter()
    plan = ProcessingPlan.create(
        source_catalog=catalog.reference,
        base_release=None,
        profiles=ProfileRegistry.builtin().local_profiles(),
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        stages=stage_policy(
            extractor=extractor, segmenter=segmenter, processor_ids=(processor.description.processor_id,),
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
        extractor=extractor,
        segmenter=segmenter,
        processors={processor.description.processor_id: processor},
    )
    plan_values = {
        field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"
    }
    capture_plan = ProcessingPlan.create(**(plan_values | {
        "stages": stage_policy(stop_after="capture"), "processors": ProcessorSet(()),
    }))
    capture_settings = {key: value for key, value in settings.items() if key not in {"extractor", "segmenter", "processors"}}
    with prepare_local_run(capture_plan, workspace, **capture_settings) as capture:
        capture_run = capture.run()
        captured_base = capture.retain(capture_run)
        capture_receipt = RunReceipt.from_dict(LocalJsonControlRepository(workspace.roots["controlRepository"]).load(capture_run))
        captured_counts = {layer.layer_kind: layer.record_count for layer in capture_receipt.staged_layers}
        assert captured_counts["files"] == 1
        assert captured_counts["representations"] == captured_counts["segments"] == 0
    assert fetcher.calls == 1
    assert extractor.calls == segmenter.calls == processor.calls == 0
    plan = ProcessingPlan.create(**(plan_values | {"base_release": captured_base}))
    prepared = prepare_local_run(plan, workspace, **settings)
    first = prepared.run()
    processed_result = prepared.retain(first)
    assert processed_result != captured_base
    assert fetcher.calls == extractor.calls == segmenter.calls == processor.calls == 1
    recovered = prepare_local_run(plan, workspace, handoff_ref=prepared.handoff_ref, **settings)
    assert recovered.run() == first
    assert fetcher.calls == extractor.calls == segmenter.calls == processor.calls == 1

    # Unified result inspection remains separate work. Inspect real retained
    # output through the existing public storage adapter in this qualification.
    run = RunReceipt.from_dict(LocalJsonControlRepository(workspace.roots["controlRepository"]).load(first))
    counts = {layer.layer_kind: layer.record_count for layer in run.staged_layers}
    assert run.selected_item_count == 1
    assert counts["files"] == counts["representations"] == counts["segments"] == 1
    assert counts["failures"] == 0
    records = LocalJsonlRecordStorage(workspace.roots["recordStorage"])
    for layer in run.staged_layers:
        if layer.layer_kind == "representations":
            row = tuple(records.stream(layer))[0]["payload"]
            assert (row["extractorId"], row["configurationDigest"]) == (
                extractor.extractor_id, extractor.configuration_digest,
            )
        elif layer.layer_kind == "segments":
            row = tuple(records.stream(layer))[0]["payload"]
            assert (row["segmenterId"], row["policyDigest"]) == (segmenter.segmenter_id, segmenter.policy_digest)

    for stage, attribute in ((extractor, "configuration_digest"), (segmenter, "policy_digest")):
        original = getattr(stage, attribute)
        setattr(stage, attribute, identity_digest({"differentStageSettings": True}))
        try:
            prepare_local_run(plan, workspace, handoff_ref=prepared.handoff_ref, **settings)
        except ProfileError:
            pass
        else:
            raise AssertionError("changed stage settings reused a saved handoff")
        setattr(stage, attribute, original)

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
    assert fetcher.calls == extractor.calls == segmenter.calls == processor.calls == 1
    assert not (Path.cwd() / "plan.json").exists()
    assert not (Path.cwd() / "run-request.json").exists()
    print("installed runtime: retained capture, processed it without refetching, recovered, refused changed settings")


if __name__ == "__main__":
    main()
