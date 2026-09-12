"""Exercise the installed runtime using the copied offline source fixture.

This runs in an isolated wheel environment, imports no test helpers, and writes
no caller-side plan or run-request files.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from rulespec_artifacts import Producer
from rulespec_artifacts.resources import canonical_json_corpus

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.identity import canonical_json_bytes, identity_digest, sha256_digest
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.errors import IntegrityError, ProfileError
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload, verify_representation_evidence, verify_segment_evidence
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import local_execution_limits, build_local_catalog, open_local_catalog, open_local_inspection, prepare_local_experiment, prepare_local_run
from docspec.source_catalog import (
    SourceCatalogCandidate,
    SuppliedRecordCatalogPolicy,
    SuppliedRecordSource,
)
from docspec.workspace import LocalWorkspace


def main() -> None:
    for case in canonical_json_corpus()["encodeAccepted"]:
        assert canonical_json_bytes(case["value"]) == bytes.fromhex(case["canonicalHex"])
    source_root = Path.cwd() / "examples" / "offline"
    source_bytes = (source_root / "notice.html").read_bytes() + b'\n<!-- {"wide":9223372036854775808} -->\n'
    numeric_document = source_root / "numeric-evidence.html"
    numeric_document.write_bytes(source_bytes)
    namespace = "urn:example:installed-markup"
    source = SuppliedRecordSource(({
        "recordId": "notice", "sourceIssuedVersion": "fixture1", "title": "Local contributor example",
        "metadata": {"synthetic": True}, "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/html", "immutable-object", numeric_document.name,
            expected_sha256=sha256_digest(source_bytes), expected_byte_size=len(source_bytes),
        ).to_dict()],
    },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
        max_records=1, max_bytes=4096)
    implementation_id = "urn:docspec:installed-runtime-test:" + sys.argv[1]
    source_producer = Producer(
        "docspec", implementation_id, "urn:docspec:verifier:source-catalog", "1.0.0", implementation_id,
    )
    release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
    workspace = LocalWorkspace(Path.cwd() / "dataset")
    catalog = build_local_catalog(
        (source,), workspace,
        policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id="urn:docspec:installed-runtime-test:catalog", producer=source_producer,
        max_scratch_bytes=8 * 1024**2,
    )
    supplied_source = SuppliedRecordSource(({
        "recordId": "note", "sourceIssuedVersion": "draft-1", "title": "Catalog-only note",
        "metadata": {"authorSupplied": "unchanged"}, "candidateRenditions": [],
    },), source_system_id="urn:example:installed-notes", source_system_version="1",
        source_state_scope="complete-snapshot", max_records=2, max_bytes=1024**2)
    supplied_workspace = LocalWorkspace(Path.cwd() / "supplied-dataset")
    supplied_catalog = build_local_catalog(
        (supplied_source,), supplied_workspace,
        policy=SuppliedRecordCatalogPolicy("urn:example:installed-notes", "1"),
        catalog_id="urn:example:installed-notes:catalog", producer=source_producer, max_scratch_bytes=8 * 1024**2,
    )
    supplied_rows = tuple(open_local_catalog(supplied_catalog.reference, supplied_workspace, producer=source_producer).iter_mappings())
    assert supplied_rows[0]["selection"]["disposition"] == "unavailable"
    assert supplied_rows[0]["sourceNativeFacts"][0]["fields"]["metadata"] == {"authorSupplied": "unchanged"}
    assert {path.name for path in supplied_workspace.root.iterdir()} == {"sourceCatalog"}
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

    class CountingFetcher(LocalFileContentFetcher):
        calls = 0

        def fetch(self, candidate, **kwargs):
            self.calls += 1
            return super().fetch(candidate, **kwargs)

    fetcher = CountingFetcher(source_root)
    experiment_settings = dict(
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        source_catalog_producer=source_producer,
        document_release_producer=release_producer,
        deadline_epoch_seconds=4_000_000_000,
        completed_at="2026-09-11T12:00:00Z",
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
    assert source_bytes == numeric_document.read_bytes()
    assert b'{"wide":9223372036854775808}' in source_bytes
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
        "execution_limits": local_execution_limits(),
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

    original_chunk_size = fetcher.chunk_size
    fetcher.chunk_size //= 2
    try:
        prepare_local_run(plan, workspace, handoff_ref=prepared.handoff_ref, **settings)
    except IntegrityError:
        pass
    else:
        raise AssertionError("changed fetcher configuration reused a saved handoff")
    fetcher.chunk_size = original_chunk_size
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
