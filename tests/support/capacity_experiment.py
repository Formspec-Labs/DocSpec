"""Ordinary local workload recipe; native tools own measurement and interruption.

Copy this file beside ``examples/phrase_match_processor.py`` outside the checkout.
Run generate, build, verify, capture, process, changed, clean, inspect and compare
as separate Python processes. Alternatively replace process with prefix followed
by resume: prefix completes some native tasks, then exits without reconciliation.
This is saved-task recovery, not cancellation during an active document stage.

Generate requires --workload, --wheel, --completed-at and --deadline. Later
operations use those saved reproduction inputs and existing native references.
Set TMPDIR before starting Python if scratch must live in the measured directory.
No timings, RSS claims, capacity verdicts, scheduler, or experiment ledger live here.
The text16 and markup16 options are development smoke fixtures, not capacity candidates.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import replace
import hashlib
import html
from importlib.metadata import version
import json
from pathlib import Path
import re
from tempfile import gettempdir

from rulespec_artifacts import Producer

from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, sha256_digest
from docspec.domain.plans import ProcessingPlan, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorResourceIdentity, ProcessorResourceKind
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, SourceCatalogRef
from docspec.processing import ParagraphSegmenter, TextExtractor
from docspec.processing.artifacts import IDENTITY_TRANSFORM
from docspec.processing.visible_text_runtime import VISIBLE_TEXT_BLOCK_TRANSFORM, VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import (
    build_local_catalog, local_execution_limits, open_local_catalog, open_local_inspection,
    prepare_local_experiment, prepare_local_run,
)
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from examples.phrase_match_processor import PhraseMatchProcessor
import examples.phrase_match_processor as phrase_module


WORKLOADS = ("text16", "markup16", "text512", "text4096", "markup256")
PHASES = ("capture", "process", "changed", "clean")
RETRY = RetryPolicy(max_attempts=1, base_delay_milliseconds=0)
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
    assert args.workload and args.wheel and args.completed_at and args.deadline
    args.root.mkdir(parents=True, exist_ok=False)
    workspace = LocalWorkspace(args.root / "dataset")
    source_root = workspace.roots["sourceContent"]
    source_root.mkdir(parents=True)
    saved = {"workload": args.workload, "wheel": str(args.wheel.resolve()),
             "completedAt": args.completed_at, "deadlineEpochSeconds": args.deadline,
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
    original = LocalWorkspace(root / "dataset")
    workspace = original if phase != "clean" else LocalWorkspace(root / "clean", {
        "sourceCatalog": original.roots["sourceCatalog"], "sourceContent": original.roots["sourceContent"],
    })
    implementation = "urn:docspec:capacity:implementation:" + saved["implementation"]["wheelSha256"]
    source = Producer("docspec-capacity", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)
    return workspace, source, replace(source, verifier_id="urn:docspec:verifier:document-release")


def _catalog(root):
    return SourceCatalogRef.from_dict(_read(root / "catalog.json"))


def _build(root, saved):
    workspace, source_producer, _ = _environment(root, saved)
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
    raw = (root / f"vocabulary-{revision}.json").read_bytes()
    resource = ProcessorResourceIdentity("urn:docspec:capacity:vocabulary", ProcessorResourceKind.REFERENCE_DATA,
        revision, sha256_digest(raw))
    return PhraseMatchProcessor(resource, raw, retry_policy=RETRY)


def _prepared(root, saved, phase, counts, *, recover=False):
    workspace, source, release = _environment(root, saved, phase)
    fetcher = _Observed(LocalFileContentFetcher(workspace.roots["sourceContent"]), "fetch", counts)
    stages, processor = {}, None
    if phase != "capture":
        markup = saved["workload"].startswith("markup")
        stages = {
            "extractor": _Observed(VisibleTextExtractor() if markup else TextExtractor(), "extract", counts),
            "segmenter": _Observed(VisibleTextBlockSegmenter() if markup else ParagraphSegmenter(), "segment", counts),
        }
        processor = _Observed(_processor(root, phase), "process", counts)
    common = dict(source_catalog_producer=source, document_release_producer=release,
        completed_at=saved["completedAt"], deadline_epoch_seconds=saved["deadlineEpochSeconds"],
        retry_policy=RETRY, content_fetcher=fetcher, execution_limits=local_execution_limits(max_task_index_bytes=256 * 1024**2))
    directory = root / phase
    if recover:
        plan = ProcessingPlan.from_dict(_read(directory / "plan.json"))
        return prepare_local_run(plan, workspace, **common, **stages, accepted_failure_policy=AcceptedFailurePolicy(),
            processors={} if phase == "capture" else {processor.description.processor_id: processor},
            handoff_ref=ArtifactRef.from_dict(_read(directory / "handoff.json")))
    base_phase = "capture" if phase == "process" else "process" if phase == "changed" else None
    base = None if base_phase is None else DocumentReleaseRef.from_dict(_read(root / base_phase / "release.json"))
    prepared = prepare_local_experiment(_catalog(root), workspace, **common, **stages,
        limits=WorkLimits(16, 64 * 1024**2, 4096, 8192, 8192, 128 * 1024**2, 900, 1),
        partition_count=16, selection=_read(root / "selection.json"), base_release=base,
        stop_after="capture" if phase == "capture" else "processing",
        processors=() if phase == "capture" else (processor,))
    _save(directory / "plan.json", prepared.plan.to_dict())
    _save(directory / "handoff.json", prepared.handoff_ref.to_dict())
    return prepared


def _run(root, saved, phase, *, prefix=None, recover=False):
    assert not (root / phase / "run.json").exists(), "use a new workload root for another measured trial"
    counts = Counter()
    with _prepared(root, saved, phase, counts, recover=recover) as prepared:
        if prefix is not None:
            assert 0 < prefix < prepared.handoff.expected_task_count
            with closing(prepared.task_source(prepared.handoff)) as tasks:
                for _ in range(prefix):
                    prepared.execute_task(prepared.handoff, next(tasks))
            _save(root / phase / "completed-prefix.json", {"completedTasks": prefix, "observedCalls": dict(counts)})
            return {"completedTasks": prefix, "observedCalls": dict(counts)}
        run = prepared.run()
        release = prepared.retain(run)
        _save(root / phase / "run.json", run.to_dict())
        _save(root / phase / "release.json", release.to_dict())
    if phase in {"process", "changed"}:
        assert counts["fetch"] == 0
    if phase == "changed":
        assert counts["extract"] == counts["segment"] == 0
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
    return {"run": run.to_dict(), "release": release.to_dict(), "observedCalls": dict(counts)}


def _view(root, saved, phase):
    workspace, source, release = _environment(root, saved, phase)
    return open_local_inspection(ProcessingPlan.from_dict(_read(root / phase / "plan.json")), workspace,
        document_release_producer=release, source_catalog_producer=source,
        release_ref=DocumentReleaseRef.from_dict(_read(root / phase / "release.json")))


def _check(root, saved, phase, view):
    """Check complete fixture inputs and outputs, excluding execution provenance."""
    summary = view.summary(sample_limit=0)
    inventory = _inventory(saved["workload"])
    layers = summary["result"]["layers"]
    assert layers["files"] == inventory["files"] and layers["failures"] == 0
    assert layers["source-items"] == inventory["documents"]
    processing = phase != "capture"
    if not processing:
        assert layers["representations"] == layers["segments"] == 0
    else:
        assert layers["representations"] == inventory["files"] and layers["segments"] == inventory["segments"]
    processor = _processor(root, phase) if processing else None
    derived_kind = f"derived:{processor.description.processor_id}" if processing else None
    if processing:
        assert layers[derived_kind] == inventory["segments"]
    factory = LocalSqliteReconciliationWorkspaceFactory(Path(gettempdir()).resolve(),
        max_spooled_bytes=256 * 1024**2, max_record_bytes=1024**2, read_batch_size=1)
    digest = hashlib.sha256()
    checked = Counter()
    workspace, producer, _ = _environment(root, saved, phase)
    assert view.plan.source_catalog == _catalog(root)
    with factory.create() as scratch:
        catalog = open_local_catalog(_catalog(root), workspace, producer=producer)
        expected_count = 0
        with closing(catalog.open_snapshot().items) as items:
            for item in items:
                if not item.document_id.startswith("excluded-"):
                    expected = item.to_processing_item()
                    scratch.add_record("expected-sources", identity=expected.item_id,
                        source_item_id=expected.item_id, record=expected.to_dict())
                    expected_count += 1
        assert expected_count == inventory["documents"]
        kinds = ("source-items", "dispositions", "files")
        if processing:
            kinds += ("representations", "segments", derived_kind)
        for kind in kinds:
            with closing(view.records(kind)) as rows:
                for row in rows:
                    payload = row["payload"]
                    collection = "segments:" + payload["fileId"] if kind == "segments" else kind
                    identity = row["recordId"]
                    if kind == "representations":
                        identity = payload["fileId"]
                    elif kind == "dispositions":
                        identity = row["sourceItemId"]
                    elif kind.startswith("derived:"):
                        identity = payload["value"]["segmentId"]
                    scratch.add_record(collection, identity=identity, source_item_id=row["sourceItemId"], record=payload)
        for source in scratch.stream_records("source-items"):
            assert canonical_json_bytes(source) == canonical_json_bytes(scratch.lookup_record("expected-sources", source["itemId"]))
            outcome = scratch.lookup_record("dispositions", source["itemId"])
            assert outcome["disposition"] == "captured" and outcome["warnings"] == [] and outcome["terminalFailure"] is None
            assert outcome["requestedStages"] == view.plan.stages.to_dict()
            # Entry identity/change classify the trial; they are not output equality.
            digest.update(canonical_json_file_bytes({"source": source, "outcome": {
                key: outcome[key] for key in ("disposition", "warnings", "terminalFailure", "requestedStages")
            }}))
            checked["documents"] += 1
        for captured in scratch.stream_records("files"):
            source = scratch.lookup_record("source-items", captured["sourceItemId"])
            index = int(source["metadata"]["documentId"].rsplit("-", 1)[1])
            assert 0 <= index < inventory["documents"]
            candidate = next(value for value in source["candidates"] if value["candidateId"] == captured["candidateId"])
            raw = b"".join(view.read_blob(BlobRef.from_dict(captured["blob"]), max_bytes=MAX_FILE_BYTES))
            shape = _candidates(saved["workload"], index)
            _name, size, blocks = next(item for item in shape if item[0] == captured["candidateId"])
            markup = saved["workload"].startswith("markup")
            assert raw == _body(index, captured["candidateId"], size, blocks, markup)
            assert len(raw) == candidate["expectedSize"]
            digest.update(canonical_json_file_bytes({"file": {key: captured[key]
                for key in ("sourceItemId", "sourceVersion", "fileId", "candidateId", "blob", "mediaType")}}))
            checked["files"] += 1
            if not processing:
                continue
            raw_blocks = list(re.finditer(rb"<p>(.*?)</p>", raw)) if markup else []
            expected_blocks = ([html.unescape(match.group(1).decode()).encode() for match in raw_blocks]
                               if markup else raw.split(b"\n\n"))
            representation = scratch.lookup_record("representations", captured["fileId"])
            assert representation["sourceItemId"] == captured["sourceItemId"]
            assert representation["fileDigest"] == captured["blob"]["digest"]
            represented = b"".join(view.read_blob(BlobRef.from_dict(representation["blob"]), max_bytes=MAX_FILE_BYTES))
            assert represented == b"\n\n".join(expected_blocks)
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
            seen = set()
            for segment in scratch.stream_records("segments:" + captured["fileId"]):
                ordinal = segment["ordinal"]
                assert ordinal not in seen
                seen.add(ordinal)
                content = b"".join(view.read_blob(BlobRef.from_dict(segment["content"]), max_bytes=64 * 1024))
                assert content == expected_blocks[ordinal]
                evidence = segment["evidence"]
                assert evidence["sourceDigest"] == captured["blob"]["digest"]
                span = raw[evidence["start"]:evidence["end"]]
                assert (html.unescape(span.decode()).encode() if markup else span) == content
                derived = scratch.lookup_record(derived_kind, segment["segmentId"])
                assert derived["sourceItemId"] == captured["sourceItemId"]
                assert derived["processorId"] == processor.description.processor_id
                assert derived["inputIds"] == [segment["segmentId"]]
                value = derived["value"]
                matches = []
                for term in ("privacy", "security") if phase in {"changed", "clean"} else ("privacy",):
                    start = content.lower().index(term.encode())
                    matches.append({"termId": term, "label": term.title(), "phrase": term,
                        "quote": content[start:start + len(term)].decode(), "segmentByteStart": start,
                        "segmentByteEnd": start + len(term)})
                expected = {"segmentId": segment["segmentId"], "segmentDigest": sha256_digest(content),
                    "resource": processor.description.external_resources[0].to_dict(),
                    "enclosingSourceEvidence": evidence, "matches": sorted(matches, key=lambda match: match["segmentByteStart"])}
                assert canonical_json_bytes(value) == canonical_json_bytes(expected)
                digest.update(canonical_json_file_bytes({"segment": segment, "value": value}))
                checked["segments"] += 1
            assert seen == set(range(blocks))
    assert checked["documents"] == inventory["documents"] and checked["files"] == inventory["files"]
    assert checked["segments"] == (inventory["segments"] if processing else 0)
    return {"inventory": dict(inventory), "checked": dict(checked), "logicalStreamSha256": "sha256:" + digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("generate", "build", "verify", *PHASES, "prefix", "resume", "inspect", "check", "compare"))
    parser.add_argument("root", type=lambda value: Path(value).resolve())
    parser.add_argument("--workload", choices=WORKLOADS)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--completed-at")
    parser.add_argument("--deadline", type=int)
    parser.add_argument("--phase", choices=PHASES, default="process")
    parser.add_argument("--prefix-tasks", type=int, default=16)
    args = parser.parse_args()
    if args.operation == "generate":
        result = _generate(args)
    else:
        assert not any((args.workload, args.wheel, args.completed_at, args.deadline)), "generation arguments are already saved"
        saved = _read(args.root / "arguments.json")
        assert _implementation(Path(saved["wheel"])) == saved["implementation"], "wheel or processor changed"
        if args.operation == "build":
            result = _build(args.root, saved)
        elif args.operation == "verify":
            workspace, source, _ = _environment(args.root, saved)
            summary = SourceCatalogArtifactReader(LocalSourceCatalogStore(workspace.roots["sourceCatalog"], create=False),
                producer=source).verify_snapshot(_catalog(args.root))
            assert summary.item_count == sum(1 for _ in _population(saved["workload"]))
            result = {"catalog": _catalog(args.root).to_dict(), "sourceItems": summary.item_count}
        elif args.operation in PHASES or args.operation in {"prefix", "resume"}:
            phase = "process" if args.operation in {"prefix", "resume"} else args.operation
            result = _run(args.root, saved, phase, prefix=args.prefix_tasks if args.operation == "prefix" else None,
                recover=args.operation == "resume")
        elif args.operation == "inspect":
            with _view(args.root, saved, args.phase) as view:
                result = view.summary(sample_limit=0)
        elif args.operation == "check":
            with _view(args.root, saved, args.phase) as view:
                result = _check(args.root, saved, args.phase, view)
        else:
            with _view(args.root, saved, "changed") as changed_view, _view(args.root, saved, "clean") as clean_view:
                changed = _check(args.root, saved, "changed", changed_view)
                clean = _check(args.root, saved, "clean", clean_view)
                assert changed == clean, "full clean and reused value/evidence streams differ"
                result = {"checked": changed, "comparison": changed_view.compare(clean_view, sample_limit=0)}
    print(canonical_json_file_bytes(result).decode(), end="")


if __name__ == "__main__":
    main()
