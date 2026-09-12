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

from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.identity import identity_digest
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.errors import IntegrityError, ProfileError
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload, verify_representation_evidence, verify_segment_evidence
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import open_local_inspection, prepare_local_experiment, prepare_local_run
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

    extraction_calls = []

    class InstalledExtractor(VisibleTextExtractor):

        def extract(self, *args):
            extraction_calls.append(args[0].file_id)
            return super().extract(*args)

    class InstalledSegmenter(VisibleTextBlockSegmenter):
        calls = 0

        def segment(self, *args):
            self.calls += 1
            return super().segment(*args)

    extractor, segmenter = InstalledExtractor(), InstalledSegmenter()

    class CountingFetcher(example["ExampleFetcher"]):
        calls = 0

        def fetch(self, candidate, **kwargs):
            self.calls += 1
            return super().fetch(candidate, **kwargs)

    fetcher = CountingFetcher(source)
    experiment_settings = dict(
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        source_catalog_producer=source_producer,
        document_release_producer=release_producer,
        deadline_epoch_seconds=4_000_000_000,
        completed_at=example["COMPLETED_AT"],
        content_fetcher=fetcher,
    )
    with prepare_local_experiment(catalog.reference, workspace, stop_after="capture", **experiment_settings) as capture:
        capture_run = capture.run()
        captured_base = capture.retain(capture_run)
        captured_view = open_local_inspection(
            capture.plan, workspace, document_release_producer=release_producer, release_ref=captured_base,
        )
        captured_counts = captured_view.summary()["result"]["layers"]
        assert captured_counts["files"] == 1
        assert captured_counts["representations"] == captured_counts["segments"] == 0
    assert fetcher.calls == 1
    assert len(extraction_calls) == segmenter.calls == processor.calls == 0
    processing_settings = experiment_settings | {
        "base_release": captured_base, "extractor": extractor, "segmenter": segmenter, "processors": (processor,),
    }
    prepared = prepare_local_experiment(catalog.reference, workspace, **processing_settings)
    plan = prepared.plan
    first = prepared.run()
    processed_result = prepared.retain(first)
    assert processed_result != captured_base
    assert fetcher.calls == len(extraction_calls) == segmenter.calls == 1
    assert processor.calls == 3
    recovered = prepare_local_experiment(catalog.reference, workspace, handoff_ref=prepared.handoff_ref, **processing_settings)
    assert recovered.run() == first
    assert fetcher.calls == len(extraction_calls) == segmenter.calls == 1
    assert processor.calls == 3

    inspected = open_local_inspection(
        plan, workspace, document_release_producer=release_producer,
        source_catalog_producer=source_producer, release_ref=processed_result,
    )
    summary = inspected.summary()
    counts = summary["result"]["layers"]
    assert summary["work"]["counts"]["scheduledItems"] == 1
    assert summary["work"]["counts"]["newCapturedFiles"] == 0
    assert summary["work"]["counts"]["reusedCapturedFiles"] == 1
    assert summary["source"]["itemCount"] == 1
    assert counts["files"] == counts["representations"] == 1
    assert counts["segments"] == 3
    assert counts["failures"] == 0
    comparison = captured_view.compare(inspected)
    assert comparison["configurationChanges"]
    assert comparison["result"]["changeCount"] == 1
    captured = CapturedFile.from_dict(tuple(inspected.records("files"))[0]["payload"])
    representation = Representation.from_dict(tuple(inspected.records("representations"))[0]["payload"])
    source_bytes = b"".join(inspected.read_blob(captured.blob, max_bytes=captured.blob.byte_size))
    content = b"".join(inspected.read_blob(representation.blob, max_bytes=representation.blob.byte_size))
    assert source_bytes == source.payload
    assert b"<html" not in content and "café".encode() in content
    assert (representation.extractor_id, representation.configuration_digest) == extractor.selected_identity(captured)
    assert representation.extractor_id != extractor.extractor_id
    payload = RepresentationPayload(representation, content)
    resolver = extractor.evidence_resolver(captured, source_bytes)
    verify_representation_evidence(payload, source_bytes, derived_resolver=resolver)
    for row in inspected.records("segments"):
        segment = Segment.from_dict(row["payload"])
        segment_bytes = b"".join(inspected.read_blob(segment.content, max_bytes=segment.content.byte_size))
        assert (segment.segmenter_id, segment.policy_digest) == segmenter.selected_identity(representation)
        verify_segment_evidence(SegmentPayload(segment, segment_bytes), payload, source_bytes, derived_resolver=resolver)

    settings = {
        **{key: value for key, value in experiment_settings.items() if key != "limits"},
        "retry_policy": retry, "accepted_failure_policy": accepted,
        "execution_limits": prepared.execution_profile.limits,
        "extractor": extractor, "segmenter": segmenter,
        "processors": {processor.description.processor_id: processor},
    }
    changed_segmenter = VisibleTextBlockSegmenter()
    changed_segmenter.policy_digest = identity_digest({"differentStageSettings": True})
    for changed_stages in (
        {"extractor": VisibleTextExtractor(html_heading_tags={"h1": 2})},
        {"segmenter": changed_segmenter},
    ):
        try:
            prepare_local_run(plan, workspace, handoff_ref=prepared.handoff_ref, **(settings | changed_stages))
        except ProfileError:
            pass
        else:
            raise AssertionError("changed stage settings reused a saved handoff")

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
    assert fetcher.calls == len(extraction_calls) == segmenter.calls == 1
    assert processor.calls == 3
    assert not (Path.cwd() / "plan.json").exists()
    assert not (Path.cwd() / "run-request.json").exists()
    print("installed runtime: small configuration retained capture, processed it without refetching, recovered, refused changed settings")


if __name__ == "__main__":
    main()
