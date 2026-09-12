"""Plan per-item reuse and refuse invalid retained prefixes before new work."""

from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.application.base_reprocessing import prepare_base_reprocessing
from docspec.application.planner import RunPlanner
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import identity_digest, sha256_digest
from docspec.domain.jobs import DocumentEntry, EntryExecutionMode
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorSet
from docspec.domain.release import DocumentRelease
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
from tests.helpers import EMPTY_DIGEST, SharedFixtureContentFetcher, artifact
from tests.support.planner import MemoryControls, MemorySourceCatalog, MemoryStores
from tests.support.profiles import _seeded_local_run


def _replan(plan, **changes):
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


def test_mixed_stage_items_use_their_own_retained_policy(tmp_path):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    template = _local_run_arguments(_local_run_request(path))["plan"]
    complete = stage_policy(extractor=TextExtractor(), segmenter=ParagraphSegmenter())
    policies = {
        "a-captured": stage_policy(stop_after="capture"),
        "b-extracted": stage_policy(stop_after="extraction", extractor=TextExtractor()),
        "c-current": complete,
        "d-old-extraction": replace(complete, extractor_configuration_digest=identity_digest({"old": "extractor"})),
        "e-old-segmentation": replace(complete, segmenter_policy_digest=identity_digest({"old": "segmenter"})),
    }
    items = tuple(SourceItem(identifier, "v1", (CandidateFile(
        "primary", "fixture://source", "text/plain", expected_size=1,
    ),)) for identifier in policies)
    controls, stores = MemoryControls(), MemoryStores()
    previous = _replan(template, stages=complete, processors=ProcessorSet(()))
    previous_ref = controls.put(kind="plans", artifact_id=previous.plan_id, value=previous.to_dict())
    release = DocumentRelease.create(
        release_id="urn:spicy:artifact:derivation:" + "d" * 64,
        previous_release=None, source_catalog=previous.source_catalog, processing_plan=previous_ref,
        profiles=previous.profiles, active_layers=(), blob_roots=(),
        retention_dispositions=previous.retention_policy, store_receipt_set_digest=EMPTY_DIGEST,
        run_receipt=artifact("run"), catalog_commit_receipt=artifact("commit"),
        counts={"sourceItems": len(items)}, failures={}, coverage={},
        partition_policy={"policyId": "test", "bucketCount": previous.partition_count},
    )
    reference = release.reference("memory://base", sha256_digest(release.file_bytes))
    plan = _replan(previous, base_release=reference)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())

    def scan(*, layer_kind):
        for item in items:
            payload = item.to_dict() if layer_kind == "source-items" else {
                "entryId": f"entry:{item.item_id}", "change": "added", "disposition": "captured",
                "warnings": [], "requestedStages": policies[item.item_id].to_dict(), "terminalFailure": None,
            }
            yield {
                "recordId": item.item_id, "sourceItemId": item.item_id,
                "idempotencyKey": item.item_id, "deleted": False, "payload": payload,
            }

    reader = SimpleNamespace(release=release, scan=scan)
    catalog = SimpleNamespace(open_reader=lambda ref: reader)
    planned = tuple(RunPlanner(
        source_catalog=MemorySourceCatalog(plan.source_catalog, items), document_catalog=catalog,
        stores=stores, controls=controls,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(tmp_path / "planning", read_batch_size=1),
    ).plan_run(plan.source_catalog, reference, plan_ref))
    entries = [entry for ref in planned for entry in stores.load(ref).entries]
    assert {entry.source_item.item_id: entry.execution_mode for entry in entries} == {
        "a-captured": EntryExecutionMode.FROM_CAPTURES,
        "b-extracted": EntryExecutionMode.FROM_REPRESENTATIONS,
        "d-old-extraction": EntryExecutionMode.FROM_CAPTURES,
        "e-old-segmentation": EntryExecutionMode.FROM_REPRESENTATIONS,
    }
    assert all(entry.requested_stages == complete for entry in entries)
    assert all(entry.processor_ids_to_run == () for entry in entries)


@pytest.mark.parametrize("invalid", ["missing-file", "repeated-file", "wrong-source", "wrong-extraction"])
def test_invalid_base_prefix_refuses_before_new_receipts(tmp_path, monkeypatch, invalid):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(path))
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(), processors=ProcessorSet(()))
    with prepare_local_run(**arguments) as initial:
        base = initial.retain(initial.run())
    extractor = TextExtractor()
    arguments["extractor"] = extractor
    arguments["plan"] = _replan(arguments["plan"], base_release=base, stages=stage_policy(extractor=extractor))
    with prepare_local_run(**arguments) as prepared:
        composition = prepared._composition
        task = next(prepared.task_source(prepared.handoff))
        entry = composition.stores.load(task.input_store).entries[0]
        reader = composition.catalog.open_reader(base)

        def scan_source(*, layer_kind, source_item_id):
            for row in reader.scan_source(layer_kind=layer_kind, source_item_id=source_item_id):
                if invalid == "missing-file" and layer_kind == "files":
                    continue
                if invalid == "wrong-source" and layer_kind == "source-items":
                    row = {**row, "payload": {**row["payload"], "version": "different"}}
                yield row
                if invalid == "repeated-file" and layer_kind == "files":
                    yield row

        if invalid == "wrong-extraction":
            entry = DocumentEntry.create(
                entry.source_item, entry.change, entry.requested_stages,
                execution_mode=EntryExecutionMode.FROM_REPRESENTATIONS,
            )
        wrapped = SimpleNamespace(release=reader.release, scan_source=scan_source)
        verifier = composition.executor._checkpoints
        checkpoint = verifier.verify_entry(entry, arguments["plan"])

        def unexpected_write(**kwargs):
            pytest.fail("invalid retained input must refuse before a new receipt is written")

        monkeypatch.setattr(composition.controls, "put", unexpected_write)
        expected = {
            "missing-file": "capture population differs", "repeated-file": "reusable input bound",
            "wrong-source": "source item differs", "wrong-extraction": "extraction settings differ",
        }[invalid]
        with pytest.raises((IntegrityError, LimitExceededError), match=expected):
            prepare_base_reprocessing(
                entry, arguments["plan"], wrapped, checkpoint,
                plan_ref=composition.plan_ref, controls=composition.controls, checkpoints=verifier,
            )
