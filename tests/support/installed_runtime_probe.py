"""Qualify the installed Core runtime with capture, processing and portable evidence."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sys

from rulespec_artifacts import Producer
from rulespec_artifacts.resources import canonical_json_corpus

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.document_processors import content_statistics_processor, segment_rows
from docspec.application.documents import DocumentProcessor
from docspec.domain.content import CapturedFile, Representation
from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.source_catalog import SourceCatalogItem
from docspec.errors import IntegrityError
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload, verify_representation_evidence, verify_segment_evidence
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import CoreWorkspace, build_local_catalog, open_local_catalog
from docspec.result_export import open_result_export
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource

# The wheel probe uses isolated Python; only the copied example directory is
# added after importing the installed package.
sys.path.insert(0, str(Path(__file__).parent))
from examples.dataset_example_support import document_results, output_value


def main():
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
    },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot", max_records=1, max_bytes=4096)
    implementation_id = "urn:docspec:installed-runtime-test:" + sys.argv[1]
    producer = Producer("docspec", implementation_id, "urn:docspec:verifier:source-catalog", "1.0.0", implementation_id)
    root = Path.cwd() / "dataset"
    catalog = build_local_catalog((source,), root, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id="urn:docspec:installed-runtime-test:catalog", producer=producer, max_scratch_bytes=8 * 1024**2)
    # Metadata-only source catalogs remain portable and do not create Core stores.
    notes = SuppliedRecordSource(({
        "recordId": "note", "sourceIssuedVersion": "draft-1", "title": "Catalog-only note",
        "metadata": {"authorSupplied": "unchanged"}, "candidateRenditions": [],
    },), source_system_id="urn:example:installed-notes", source_system_version="1",
        source_state_scope="complete-snapshot", max_records=2, max_bytes=1024**2)
    notes_root = Path.cwd() / "supplied-dataset"
    notes_catalog = build_local_catalog((notes,), notes_root, policy=SuppliedRecordCatalogPolicy("urn:example:installed-notes", "1"),
        catalog_id="urn:example:installed-notes:catalog", producer=producer, max_scratch_bytes=8 * 1024**2)
    note = next(open_local_catalog(notes_catalog.reference, notes_root, producer=producer).iter_mappings())
    assert note["selection"]["disposition"] == "unavailable"
    assert note["sourceNativeFacts"][0]["fields"]["metadata"] == {"authorSupplied": "unchanged"}
    assert {path.name for path in notes_root.iterdir()} == {"sourceCatalog"}
    calls = {"capture": 0, "extract": 0, "segment": 0, "processor": 0}
    class Extractor(VisibleTextExtractor):
        def extract(self, *args):
            calls["extract"] += 1
            return super().extract(*args)
    class Segmenter(VisibleTextBlockSegmenter):
        def segment(self, *args):
            calls["segment"] += 1
            return super().segment(*args)
    class Fetcher(LocalFileContentFetcher):
        def fetch(self, *args, **kwargs):
            calls["capture"] += 1
            return super().fetch(*args, **kwargs)
    fetcher, extractor, segmenter = Fetcher(source_root), Extractor(), Segmenter()
    standard = content_statistics_processor()
    def count(context, inputs):
        calls["processor"] += 1
        return standard.process(context, inputs)
    processor = DocumentProcessor(standard.name, standard.definition, count)
    with CoreWorkspace(root) as workspace:
        pipeline = workspace.documents(fetcher=fetcher, extractor=extractor, segmenter=segmenter)
        admitted = open_local_catalog(catalog.reference, root, producer=producer)
        pipeline.import_sources((SourceCatalogItem.from_dict(row) for row in admitted.iter_mappings()), state_id="sources")
        pipeline.run("sources", run_id="captured", extract=False, segment=False)
        assert len(dict(document_results(workspace, pipeline, "captured")).popitem()[1]) == 1
        assert calls == {"capture": 1, "extract": 0, "segment": 0, "processor": 0}
    # A fresh workspace lifetime processes retained captures without acquisition.
    with CoreWorkspace(root) as workspace:
        pipeline = workspace.documents(fetcher=fetcher, extractor=extractor, segmenter=segmenter)
        pipeline.run("sources", run_id="processed", processors=(processor,))
        first = dict(document_results(workspace, pipeline, "processed"))
        pipeline.run("sources", run_id="reused", processors=(processor,), max_source_bytes=0, max_generated_rows=0)
        assert dict(document_results(workspace, pipeline, "reused")) == first
        assert calls == {"capture": 1, "extract": 1, "segment": 1, "processor": 1}
        stages = next(iter(first.values()))
        captured = CapturedFile.from_dict(output_value(workspace, stages[0], "capture"))
        representation = Representation.from_dict(output_value(workspace, stages[1], "representation")["representation"])
        content = output_value(workspace, stages[1], "content")
        assert output_value(workspace, stages[0], "content") == source_bytes
        assert b'{"wide":9223372036854775808}' in source_bytes
        assert b"<html" not in content and "café".encode() in content
        assert (representation.extractor_id, representation.configuration_digest) == extractor.selected_identity(captured)
        assert representation.extractor_id != extractor.extractor_id
        payload = RepresentationPayload(representation, content)
        resolver = extractor.evidence_resolver(captured, source_bytes)
        verify_representation_evidence(payload, source_bytes, derived_resolver=resolver)
        with workspace.publisher.session() as session:
            with closing(segment_rows(session, stages[2].outcome.outputs[0].entity_id)) as rows:
                segments = tuple(rows)
            assert len(segments) == 3
            for _, segment, reference in segments:
                with closing(workspace.blobs.read(reference, max_bytes=reference.byte_size)) as chunks:
                    segment_bytes = b"".join(chunks)
                assert (segment.segmenter_id, segment.policy_digest) == segmenter.selected_identity(representation)
                verify_segment_evidence(SegmentPayload(segment, segment_bytes), payload, source_bytes, derived_resolver=resolver)
        assert len(tuple(pipeline.rows(stages[3].outcome.outputs[0].entity_id))) == 3
        fetcher.chunk_size //= 2
        try:
            pipeline.run("sources", run_id="changed-fetcher", fresh=True)
        except IntegrityError:
            pass
        else:
            raise AssertionError("bound fetcher accepted changed configuration")
        fetcher.chunk_size *= 2
        export_producer = replace(producer, verifier_id="urn:docspec:verifier:result-export")
        destination = Path.cwd() / "independent-result"
        pin = workspace.export("processed", destination, producer=export_producer, max_output_bytes=16 * 1024**2,
            additional_roots=pipeline.retained_roots("processed"))
        assert workspace.export("processed", destination, producer=export_producer, max_output_bytes=16 * 1024**2,
            additional_roots=pipeline.retained_roots("processed")) == pin
    root.rename(root.with_name("original-dataset-unavailable"))
    numeric_document.rename(numeric_document.with_suffix(".unavailable"))
    with open_result_export(destination, expected_pin=pin, producer=export_producer, max_output_bytes=16 * 1024**2) as exported:
        assert len(tuple(exported.rows())) == 1
        assert exported.read_blob(captured.blob, max_bytes=len(source_bytes)) == source_bytes
        for stage in stages:
            assert exported.record("result", stage.result_id) == stage
        for _, _, reference in segments:
            assert exported.read_blob(reference, max_bytes=reference.byte_size)
        roots = tuple(exported.roots())
        assert ("state", "processed") in roots and any(kind == "selection" for kind, _ in roots)
    blob_path = destination / "blobs" / captured.blob.locator
    blob_path.write_bytes(b"!" + source_bytes[1:])
    try:
        with open_result_export(destination, expected_pin=pin, producer=export_producer, max_output_bytes=16 * 1024**2) as exported:
            exported.read_blob(captured.blob, max_bytes=len(source_bytes))
    except IntegrityError:
        pass
    else:
        raise AssertionError("independent reader accepted mutated source bytes")
    assert not (Path.cwd() / "plan.json").exists()
    assert not (Path.cwd() / "run-request.json").exists()
    print("installed runtime: retained capture, processed without refetching, reused exact results and verified independent evidence")


if __name__ == "__main__":
    main()
