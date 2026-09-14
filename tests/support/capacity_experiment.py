"""Ordinary Core document workload; native tools own time and memory measurement.

Copy this file and the shared dataset/phrase examples outside the checkout.
Run generate, build, verify, capture, process, changed, clean, inspect and compare
as separate Python processes. Alternatively replace process with prefix followed
by resume: inject an interruption before the next document processor operation,
then recover the retained completed choices in a fresh process. This is retained
prefix recovery, not an operating-system crash or Core checkpoint-resume test.

Generate requires --workload and --wheel. Later operations use those saved
reproduction inputs and the retained Core state references.
Set TMPDIR before starting Python if scratch must live in the measured directory.
No timings, RSS claims, capacity verdicts, scheduler, or experiment ledger live here.
The text16 and markup16 options are development smoke fixtures, not capacity candidates.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import html
from importlib.metadata import version
import json
from pathlib import Path
import re
from tempfile import gettempdir
from unittest.mock import patch

from rulespec_artifacts import Producer

from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.record_workspace import LocalSqliteRecordWorkspaceFactory
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, sha256_digest
from docspec.application.document_processors import segment_rows
from docspec.domain import core
from docspec.domain.content import SourceItemState
from docspec.domain.core_admission import record_value
from docspec.domain.references import SourceCatalogRef
from docspec.processing import ParagraphSegmenter, TextExtractor
from docspec.processing.artifacts import IDENTITY_TRANSFORM
from docspec.processing.visible_text_runtime import VISIBLE_TEXT_BLOCK_TRANSFORM, VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import CoreWorkspace, build_local_catalog, open_local_catalog
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from examples.dataset_example_support import document_results, output_value, phrase_processor
import examples.phrase_match_processor as phrase_module


WORKLOADS = ("text16", "markup16", "text512", "text4096", "markup256")
PHASES = ("capture", "process", "changed", "clean")
MAX_FILE_BYTES = 4 * 1024**2


def _read(path):
    return json.loads(path.read_bytes())


def _save(path, value):
    raw = canonical_json_file_bytes(value)
    if path.exists():
        assert path.read_bytes() == raw, f"refusing different saved input/reference: {path}"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(raw)


def _implementation(wheel):
    return {
        "docspecVersion": version("docspec"), "wheelSha256": sha256_digest(wheel.read_bytes()),
        "processorSha256": sha256_digest(Path(phrase_module.__file__).read_bytes()),
    }


def _population(workload):
    count = int(workload[6:] if workload.startswith("markup") else workload[4:])
    for index in range(count + count // 16):
        yield index, index >= count, _candidates(workload, index)


def _candidates(workload, index):
    shape = index % 16
    if workload.startswith("markup"):
        size, blocks = (32 * 1024, 4) if shape < 12 else (512 * 1024, 16) if shape < 15 else (MAX_FILE_BYTES, 128)
    else:
        size, blocks = (8 * 1024, 2) if shape < 12 else (32 * 1024, 8) if shape < 15 else (128 * 1024, 32)
    extra = (("extra", 16 * 1024, 4),) if workload.startswith("markup") and index < 32 else ()
    return (("body", size, blocks), *extra)


def _inventory(workload):
    result = Counter()
    for _index, excluded, candidates in _population(workload):
        if not excluded:
            result["documents"] += 1
            result["files"] += len(candidates)
            result["bytes"] += sum(size for _name, size, _blocks in candidates)
            result["segments"] += sum(blocks for _name, _size, blocks in candidates)
    return result


def _body(index, candidate, size, blocks, markup):
    opening, closing_tag, separator = (b"<html><body>", b"</body></html>", b"") if markup else (b"", b"", b"\n\n")
    available = size - len(opening) - len(closing_tag) - len(separator) * (blocks - 1) - (7 * blocks if markup else 0)
    parts = []
    for ordinal in range(blocks):
        length = available // blocks + (ordinal < available % blocks)
        accent = "caf&eacute;" if markup else "café"
        header = f"Document {index:06d} {candidate} block {ordinal:04d} {accent} PRIVACY security ".encode()
        assert length > len(header)
        content = header + b"x" * (length - len(header))
        parts.append(b"<p>" + content + b"</p>" if markup else content)
    result = opening + separator.join(parts) + closing_tag
    assert len(result) == size
    return result


def _generate(args):
    assert args.workload and args.wheel
    args.root.mkdir(parents=True, exist_ok=False)
    source_root = args.root / "sourceContent"
    source_root.mkdir(parents=True)
    saved = {"workload": args.workload, "wheel": str(args.wheel.resolve()),
             "implementation": _implementation(args.wheel),
             "recipeSha256": sha256_digest(Path(__file__).read_bytes())}
    _save(args.root / "arguments.json", saved)
    markup = args.workload.startswith("markup")
    with (args.root / "supplied-records.jsonl").open("xb") as output:
        for index, excluded, candidates in _population(args.workload):
            renditions = []
            for candidate, size, blocks in candidates:
                raw = _body(index, candidate, size, blocks, markup)
                locator = f"{index:06d}-{candidate}.{'html' if markup else 'txt'}"
                (source_root / locator).write_bytes(raw)
                renditions.append(SourceCatalogCandidate(candidate, "text/html" if markup else "text/plain",
                    "immutable-object", locator, expected_sha256=sha256_digest(raw), expected_byte_size=len(raw)).to_dict())
            output.write(canonical_json_file_bytes({
                "recordId": f"{'excluded' if excluded else 'document'}-{index:06d}", "sourceIssuedVersion": "1",
                "title": f"Synthetic document {index:06d}", "metadata": {"synthetic": True},
                "candidateRenditions": renditions,
            }))
    for revision, phrases in (("v1", ("privacy",)), ("v2", ("privacy", "security"))):
        _save(args.root / f"vocabulary-{revision}.json", {"terms": [
            {"id": phrase, "label": phrase.title(), "phrases": [phrase]} for phrase in phrases
        ]})
    return saved


def _environment(root, saved, phase="process"):
    workspace = root / ("clean" if phase == "clean" else "dataset")
    implementation = "urn:docspec:capacity:implementation:" + saved["implementation"]["wheelSha256"]
    source = Producer("docspec-capacity", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)
    return workspace, source


def _catalog(root):
    return SourceCatalogRef.from_dict(_read(root / "catalog.json"))


def _build(root, saved):
    workspace, source_producer = _environment(root, saved)
    namespace = f"urn:docspec:capacity:{saved['workload']}"
    with (root / "supplied-records.jsonl").open("rb") as rows:
        source = SuppliedRecordSource((json.loads(line) for line in rows), source_system_id=namespace,
            source_system_version="1", source_state_scope="complete-snapshot", max_records=5000, max_bytes=64 * 1024**2)
    result = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id=namespace + ":catalog", producer=source_producer, max_scratch_bytes=256 * 1024**2)
    _save(root / "catalog.json", result.reference.to_dict())
    # These are explicit run exclusions, not invented upstream rejection facts.
    with closing(open_local_catalog(result.reference, workspace, producer=source_producer).iter_mappings()) as rows:
        excluded = [row["sourceItemId"] for row in rows if row["documentId"].startswith("excluded-")]
    _save(root / "selection.json", {"excludeItemIds": excluded})
    return result.reference.to_dict()


class _Observed:
    """Count calls to an actual injected implementation without changing its pins."""

    def __init__(self, delegate, method, counts):
        self.delegate, self.method, self.counts = delegate, method, counts

    def __getattr__(self, name):
        attribute = getattr(self.delegate, name)
        if name != self.method:
            return attribute
        def call(*args, **kwargs):
            self.counts[name] += 1
            return attribute(*args, **kwargs)
        return call


def _processor(root, phase):
    revision = "v2" if phase in {"changed", "clean"} else "v1"
    return phrase_processor(revision, resource_id="urn:docspec:capacity:vocabulary",
        resource_bytes=(root / f"vocabulary-{revision}.json").read_bytes())


def _sources(root, saved):
    _, producer = _environment(root, saved)
    excluded = set(_read(root / "selection.json")["excludeItemIds"])
    catalog = open_local_catalog(_catalog(root), root / "dataset", producer=producer)
    with closing(catalog.open_snapshot().items) as items:
        for item in items:
            source = item.to_processing_item()
            yield replace(source, state=SourceItemState.EXCLUDED) if source.item_id in excluded else source


@contextmanager
def _pipeline(root, saved, path, counts):
    markup = saved["workload"].startswith("markup")
    original_match = phrase_module.PhraseMatcher.__call__
    def count_match(matcher, payload):
        counts["process"] += 1
        return original_match(matcher, payload)
    with CoreWorkspace(path) as workspace, patch.object(phrase_module.PhraseMatcher, "__call__", count_match):
        pipeline = workspace.documents(
            fetcher=_Observed(LocalFileContentFetcher(root / "sourceContent"), "fetch", counts),
            extractor=_Observed(VisibleTextExtractor() if markup else TextExtractor(), "extract", counts),
            segmenter=_Observed(VisibleTextBlockSegmenter() if markup else ParagraphSegmenter(), "segment", counts))
        yield workspace, pipeline


class _PrefixComplete(BaseException):
    """An injected boundary before the next producer, not an OS interruption."""


def _run(root, saved, phase, *, prefix=None, recover=False):
    assert not (root / phase / "run.json").exists(), "use a new workload root for another measured trial"
    counts = Counter()
    path, _ = _environment(root, saved, phase)
    processing = phase != "capture"
    completed = 0
    interrupted_execution = None
    processor = _processor(root, phase) if processing else None
    if prefix is not None:
        assert 0 < prefix < _inventory(saved["workload"])["files"]
        original_process = processor.process
        def prefix_process(context, inputs):
            nonlocal completed, interrupted_execution
            if completed == prefix:
                interrupted_execution = context.execution.execution_id
                raise _PrefixComplete("injected interruption after completed document processor operations")
            result = original_process(context, inputs)
            completed += 1
            return result
        processor = replace(processor, process=prefix_process)
    with _pipeline(root, saved, path, counts) as (workspace, pipeline):
        with closing(workspace.ledger.read_records([("state", "catalog")])) as records:
            imported = next(records)[0]
        if imported is None:
            pipeline.import_sources(_sources(root, saved), state_id="catalog")
        try:
            state = pipeline.run("catalog", run_id=phase, dataset="documents", extract=processing, segment=processing,
                processors=() if processor is None else (processor,))
        except _PrefixComplete:
            assert prefix is not None and completed == prefix
            with closing(workspace.ledger.read_progress(interrupted_execution)) as updates:
                progress = [json.loads(row) for batch in updates for row in batch]
            assert progress[-1]["status"] == "interrupted"
            result = {"completedProcessorOperations": completed, "interruptedExecutionId": interrupted_execution,
                "observedCalls": dict(counts),
                "interruption": "injected-before-next-producer", "recovery": "retained-prefix"}
            _save(root / phase / "completed-prefix.json", result)
            return result
        assert prefix is None, "the injected prefix boundary was not reached"
        _save(root / phase / "run.json", {"state": record_value(state), "observedCalls": dict(counts)})
    total = counts.copy()
    if recover:
        total.update(_read(root / phase / "completed-prefix.json")["observedCalls"])
    inventory = _inventory(saved["workload"])
    file_count, segment_count = inventory["files"], inventory["segments"]
    expected = {"fetch": file_count if phase in {"capture", "clean"} else 0,
        "extract": file_count if phase in {"process", "clean"} else 0,
        "segment": file_count if phase in {"process", "clean"} else 0,
        "process": 0 if phase == "capture" else segment_count}
    assert all(total[name] == value for name, value in expected.items()), (dict(total), expected)
    return {"state": record_value(state), "observedCalls": dict(counts), "totalCalls": dict(total)}


def _state_id(root, phase):
    return _read(root / phase / "run.json")["state"]["state_id"]


def _check(root, saved, phase, workspace, *, state_id=None, source_state_id="catalog", sources=None):
    """Check every value and source span independently; exclude attempt identities."""
    state_id = _state_id(root, phase) if state_id is None else state_id
    inventory = _inventory(saved["workload"])
    processing, markup = phase != "capture", saved["workload"].startswith("markup")
    processor = _processor(root, phase) if processing else None
    factory = LocalSqliteRecordWorkspaceFactory(Path(gettempdir()).resolve(),
        max_spooled_bytes=256 * 1024**2, max_record_bytes=1024**2, read_batch_size=1)
    pipeline = workspace.documents(fetcher=LocalFileContentFetcher(root / "sourceContent"))
    digest, checked = hashlib.sha256(), Counter()
    with factory.create() as scratch:
        for source in _sources(root, saved) if sources is None else sources:
            scratch.add_record("sources", identity=source.item_id, source_item_id=source.item_id, record=source.to_dict())
        with closing(pipeline.rows(source_state_id)) as imported:
            source_count = 0
            for key, entity_id, value in imported:
                scratch.add_record("source-entities", identity=key, source_item_id=key, record={"entityId": entity_id})
                assert canonical_json_bytes(value) == canonical_json_bytes(scratch.lookup_record("sources", key))
                source_count += 1
            assert source_count == inventory["documents"] + inventory["documents"] // 16
        with closing(pipeline.rows(state_id)) as summaries:
            summary_count = 0
            for key, _, summary in summaries:
                assert summary["sourceItemId"] == key
                assert summary["sourceEntityId"] == scratch.lookup_record("source-entities", key)["entityId"]
                summary_count += 1
            assert summary_count == source_count
        with closing(document_results(workspace, pipeline, state_id)) as documents:
            for key, results in documents:
                source = scratch.lookup_record("sources", key)
                assert source is not None
                if source["state"] == "excluded":
                    assert not results
                    checked["excluded"] += 1
                    continue
                assert source["state"] == "active"
                index = int(source["metadata"]["documentId"].rsplit("-", 1)[1])
                assert 0 <= index < inventory["documents"]
                stages = 4 if processing else 1
                assert len(results) == len(source["candidates"]) * stages
                assert all(result.outcome.status == "success" for result in results)
                digest.update(canonical_json_file_bytes({"source": source}))
                checked["documents"] += 1
                for position, candidate in enumerate(source["candidates"]):
                    capture = results[position * stages]
                    captured = output_value(workspace, capture, "capture")
                    assert captured["sourceItemId"] == key and captured["sourceVersion"] == source["version"]
                    assert captured["candidateId"] == candidate["candidateId"]
                    assert captured["mediaType"] == candidate["mediaType"]
                    raw = output_value(workspace, capture, "content")
                    shape = _candidates(saved["workload"], index)
                    _, size, blocks = next(item for item in shape if item[0] == captured["candidateId"])
                    assert raw == _body(index, captured["candidateId"], size, blocks, markup)
                    assert len(raw) == candidate["expectedSize"]
                    assert captured["blob"]["digest"] == candidate["expectedDigest"] == sha256_digest(raw)
                    digest.update(canonical_json_file_bytes({"file": {key: captured[key]
                        for key in ("sourceItemId", "sourceVersion", "fileId", "candidateId", "blob", "mediaType")}}))
                    checked["files"] += 1
                    if not processing:
                        continue
                    extraction, segmentation, processed = results[position * stages + 1:position * stages + 4]
                    raw_blocks = list(re.finditer(rb"<p>(.*?)</p>", raw)) if markup else []
                    expected_blocks = ([html.unescape(match.group(1).decode()).encode() for match in raw_blocks]
                        if markup else raw.split(b"\n\n"))
                    representation = output_value(workspace, extraction, "representation")["representation"]
                    assert representation["sourceItemId"] == captured["sourceItemId"]
                    assert representation["fileId"] == captured["fileId"]
                    assert representation["fileDigest"] == captured["blob"]["digest"]
                    assert output_value(workspace, extraction, "content") == b"\n\n".join(expected_blocks)
                    mappings = representation["evidenceMappings"]
                    assert len(mappings) == (blocks if markup else 1)
                    offset = 0
                    for ordinal, mapping in enumerate(mappings):
                        length = len(expected_blocks[ordinal]) if markup else len(raw)
                        start, end = raw_blocks[ordinal].span(1) if markup else (0, len(raw))
                        assert (mapping["representationStart"], mapping["representationEnd"]) == (offset, offset + length)
                        assert mapping["transformation"] == (VISIBLE_TEXT_BLOCK_TRANSFORM if markup else IDENTITY_TRANSFORM)
                        assert mapping["evidence"] == {"coordinateSystem": "utf8-byte-range",
                            "sourceDigest": captured["blob"]["digest"], "start": start, "end": end, "page": None, "region": None}
                        offset += length + 2
                    digest.update(canonical_json_file_bytes({"representation": representation}))
                    checked["representations"] += 1
                    output_state = next(item.entity_id for item in processed.outcome.outputs if item.label == "records")
                    with closing(pipeline.rows(output_state)) as outputs:
                        values = {key: value for key, _, value in outputs}
                    segment_state = next(item.entity_id for item in segmentation.outcome.outputs if item.label == "segments")
                    seen = set()
                    with workspace.publisher.session() as session, closing(segment_rows(session, segment_state)) as segments:
                        for segment_key, segment_record, reference in segments:
                            segment = segment_record.to_dict()
                            assert segment["sourceItemId"] == key and segment["fileId"] == captured["fileId"]
                            assert segment["representationId"] == representation["representationId"]
                            ordinal = segment["ordinal"]
                            assert ordinal not in seen
                            seen.add(ordinal)
                            with closing(session.blobs.read(reference, max_bytes=64 * 1024)) as chunks:
                                content = b"".join(chunks)
                            assert content == expected_blocks[ordinal]
                            evidence = segment["evidence"]
                            assert evidence["sourceDigest"] == captured["blob"]["digest"]
                            span = raw[evidence["start"]:evidence["end"]]
                            assert (html.unescape(span.decode()).encode() if markup else span) == content
                            receipt = values.pop(segment_key + ":receipt")
                            assert receipt["segmentId"] == segment["segmentId"] and receipt["outputCount"] == 1
                            value = values.pop(segment_key + ":output:0")
                            matches = []
                            for term in ("privacy", "security") if phase in {"changed", "clean"} else ("privacy",):
                                start = content.lower().index(term.encode())
                                matches.append({"termId": term, "label": term.title(), "phrase": term,
                                    "quote": content[start:start + len(term)].decode(), "segmentByteStart": start,
                                    "segmentByteEnd": start + len(term)})
                            expected = {"segmentDigest": sha256_digest(content),
                                "resource": processor.definition.resources[0].description,
                                "enclosingSourceEvidence": evidence,
                                "matches": sorted(matches, key=lambda match: match["segmentByteStart"])}
                            assert canonical_json_bytes(value) == canonical_json_bytes(expected)
                            digest.update(canonical_json_file_bytes({"segment": segment, "value": value}))
                            checked["segments"] += 1
                    assert seen == set(range(blocks)) and not values
    assert checked["documents"] == inventory["documents"] and checked["files"] == inventory["files"]
    assert checked["excluded"] == inventory["documents"] // 16
    assert checked["segments"] == (inventory["segments"] if processing else 0)
    return {"inventory": dict(inventory), "checked": dict(checked), "logicalStreamSha256": "sha256:" + digest.hexdigest()}


def _retitled_sources(root, saved):
    for source in _sources(root, saved):
        yield replace(source, metadata={**source.metadata, "title": source.metadata.get("title", "") + " (revised)"})


def _same_stage_results(workspace, pipeline, older, newer, *, changed_processor=False):
    with closing(document_results(workspace, pipeline, older)) as before, closing(
            document_results(workspace, pipeline, newer)) as after:
        documents = 0
        for (old_key, old), (new_key, new) in zip(before, after, strict=True):
            assert old_key == new_key and len(old) == len(new)
            for ordinal, (prior, selected) in enumerate(zip(old, new, strict=True)):
                assert (prior.result_id != selected.result_id) == (changed_processor and ordinal % 4 == 3)
            documents += 1
        return documents


class _InjectedExtractionFailure(Exception):
    """A temporary failure of one identified input, using the same extractor pin."""


def _behavior(root, saved):
    """Exercise document repair and reuse in separate storage over the frozen inputs."""
    path = root / "behavior"
    assert not path.exists(), "use a new behavior workspace for another trial"
    inventory, observations = _inventory(saved["workload"]), {}
    processor_a, processor_b = _processor(root, "process"), _processor(root, "changed")
    failed_counts = Counter()
    with _pipeline(root, saved, path, failed_counts) as (workspace, pipeline):
        pipeline.import_sources(_sources(root, saved), state_id="catalog")
        with closing(pipeline.rows("catalog")) as sources:
            _, _, first = next(row for row in sources if row[2]["state"] == "active")
        broken = first["itemId"], first["candidates"][0]["candidateId"]
        extractor = pipeline.extractor.delegate
        original_extract = type(extractor).extract
        def fail_input(instance, captured, content):
            if (captured.source_item_id, captured.candidate_id) == broken:
                raise _InjectedExtractionFailure("injected temporary extraction failure for one retained input")
            return original_extract(instance, captured, content)
        try:
            with patch.object(type(extractor), "extract", fail_input):
                pipeline.run("catalog", run_id="failed", dataset="documents", processors=(processor_a,))
        except _InjectedExtractionFailure:
            assert workspace.ledger.current("documents") is None
        else:
            raise AssertionError("the failing input was not exercised")
        # The failure is the first extraction in a bounded work group. Capture
        # results already published by the shared owner must survive reopening.
        retained_captures, failed_result_ids = set(), []
        with closing(workspace.ledger.retained_records()) as records:
            for batch in records:
                for row in batch:
                    if isinstance(row.value, core.Result):
                        retained_captures.add(row.value.result_id)
                    elif isinstance(row.value, core.Execution):
                        with closing(workspace.ledger.read_progress(row.value.execution_id)) as updates:
                            progress = [json.loads(value) for group in updates for value in group]
                        if progress and progress[-1]["status"] == "failed":
                            failed_result_ids.append(progress[-1]["description"]["result_id"])
        assert failed_result_ids
        assert 0 < len(retained_captures) <= 32
        assert failed_counts["fetch"] == len(retained_captures)
        assert failed_counts["extract"] == 1 and not failed_counts["segment"] and not failed_counts["process"]
        observations["failure"] = {"sourceItemId": broken[0], "candidateId": broken[1],
            "errorType": "_InjectedExtractionFailure", "failedResultIds": failed_result_ids,
            "retainedCaptures": len(retained_captures),
            "observedCalls": dict(failed_counts)}
    repaired_counts = Counter()
    with _pipeline(root, saved, path, repaired_counts) as (workspace, pipeline):
        pipeline.run("catalog", run_id="repaired-a", dataset="documents", processors=(processor_a,))
        outstanding = retained_captures.copy()
        with closing(document_results(workspace, pipeline, "repaired-a")) as documents:
            for _, results in documents:
                outstanding.difference_update(result.result_id for result in results[::4])
        assert not outstanding
        with closing(workspace.ledger.read_records(("result", identifier) for identifier in failed_result_ids)) as failures:
            assert all(row.value.outcome.status == "failed" and not row.retained for batch in failures for row in batch)
        total = failed_counts + repaired_counts
        assert total == Counter(fetch=inventory["files"], extract=inventory["files"] + 1,
            segment=inventory["files"], process=inventory["segments"]), dict(total)
        observations["repair"] = {"observedCalls": dict(repaired_counts), "totalCalls": dict(total),
            "retainedCapturesReused": len(retained_captures),
            "checked": _check(root, saved, "process", workspace, state_id="repaired-a")}
    metadata_counts = Counter()
    with _pipeline(root, saved, path, metadata_counts) as (workspace, pipeline):
        pipeline.import_sources(_retitled_sources(root, saved), state_id="retitled-catalog")
        pipeline.run("retitled-catalog", run_id="retitled-a", processors=(processor_a,))
        assert not metadata_counts
        count = _same_stage_results(workspace, pipeline, "repaired-a", "retitled-a")
        assert count == inventory["documents"] + inventory["documents"] // 16
        metadata_check = _check(root, saved, "process", workspace, state_id="retitled-a",
            source_state_id="retitled-catalog", sources=_retitled_sources(root, saved))
        observations["metadataRevision"] = {"observedCalls": dict(metadata_counts),
            "allStageResultsUnchanged": True, "checked": metadata_check}
    changed_counts = Counter()
    with _pipeline(root, saved, path, changed_counts) as (workspace, pipeline):
        pipeline.run("retitled-catalog", run_id="changed-b", processors=(processor_b,))
        assert changed_counts == Counter(process=inventory["segments"])
        _same_stage_results(workspace, pipeline, "retitled-a", "changed-b", changed_processor=True)
        observations["resourceB"] = {"observedCalls": dict(changed_counts),
            "checked": _check(root, saved, "changed", workspace, state_id="changed-b",
                source_state_id="retitled-catalog", sources=_retitled_sources(root, saved))}
    returned_counts = Counter()
    with _pipeline(root, saved, path, returned_counts) as (workspace, pipeline):
        pipeline.run("retitled-catalog", run_id="returned-a", processors=(processor_a,))
        assert not returned_counts
        _same_stage_results(workspace, pipeline, "retitled-a", "returned-a")
        returned_check = _check(root, saved, "process", workspace, state_id="returned-a",
            source_state_id="retitled-catalog", sources=_retitled_sources(root, saved))
        assert returned_check == metadata_check
        observations["returnedA"] = {"observedCalls": dict(returned_counts),
            "originalAResultsSelected": True, "checked": returned_check}
    _save(root / "behavior-report.json", observations)
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("generate", "build", "verify", *PHASES, "prefix", "resume", "inspect", "check", "compare", "behavior"))
    parser.add_argument("root", type=lambda value: Path(value).resolve())
    parser.add_argument("--workload", choices=WORKLOADS)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--phase", choices=PHASES, default="process")
    parser.add_argument("--prefix-tasks", type=int, default=16)
    args = parser.parse_args()
    if args.operation == "generate":
        result = _generate(args)
    else:
        assert not any((args.workload, args.wheel)), "generation arguments are already saved"
        saved = _read(args.root / "arguments.json")
        assert _implementation(Path(saved["wheel"])) == saved["implementation"], "wheel or processor changed"
        if args.operation == "build":
            result = _build(args.root, saved)
        elif args.operation == "verify":
            workspace, source = _environment(args.root, saved)
            summary = SourceCatalogArtifactReader(LocalSourceCatalogStore(workspace / "sourceCatalog", create=False),
                producer=source).verify_snapshot(_catalog(args.root))
            assert summary.item_count == sum(1 for _ in _population(saved["workload"]))
            result = {"catalog": _catalog(args.root).to_dict(), "sourceItems": summary.item_count}
        elif args.operation == "behavior":
            result = _behavior(args.root, saved)
        elif args.operation in PHASES or args.operation in {"prefix", "resume"}:
            phase = "process" if args.operation in {"prefix", "resume"} else args.operation
            result = _run(args.root, saved, phase, prefix=args.prefix_tasks if args.operation == "prefix" else None,
                recover=args.operation == "resume")
        elif args.operation in {"inspect", "check"}:
            path, _ = _environment(args.root, saved, args.phase)
            with CoreWorkspace(path) as workspace:
                result = (workspace.inspect("state", _state_id(args.root, args.phase)) if args.operation == "inspect"
                    else _check(args.root, saved, args.phase, workspace))
        else:
            with CoreWorkspace(args.root / "dataset") as workspace:
                changed = _check(args.root, saved, "changed", workspace)
            with CoreWorkspace(args.root / "clean") as workspace:
                clean = _check(args.root, saved, "clean", workspace)
            assert changed == clean, "full clean and reused value/evidence streams differ"
            result = {"checked": changed, "equal": True}
    print(canonical_json_file_bytes(result).decode(), end="")


if __name__ == "__main__":
    main()
