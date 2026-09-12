"""Configured stages govern real execution, reuse, and checkpoint admission."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from docspec.application.commit import ReleaseCommitService
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.identity import identity_digest, stable_urn
from docspec.domain.jobs import ChangeKind, EntryExecutionMode, FailureClass
from docspec.domain.plans import ProcessingPlan
from docspec.errors import IntegrityError, ProfileError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run, stage_policy
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


class _ConfiguredText(TextExtractor):
    extractor_id = "tests.configured-source-text/v1"

    def __init__(self, variant="original"):
        self.variant = variant
        self.calls = 0

    @property
    def configuration_digest(self):
        return identity_digest({"variant": self.variant, "mode": "source-native-passthrough"})

    def extract(self, captured, source_bytes):
        self.calls += 1
        return super().extract(captured, source_bytes)


class _ConfiguredParagraphs(ParagraphSegmenter):
    segmenter_id = "tests.optional-paragraphs/v1"

    def __init__(self, include=True):
        self.include = include
        self.calls = 0

    @property
    def policy_digest(self):
        return identity_digest({"includeParagraphs": self.include})

    def segment(self, representation):
        self.calls += 1
        return super().segment(representation) if self.include else ()


def _replan(plan, **changes):
    values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
    return ProcessingPlan.create(**(values | changes))


@pytest.fixture
def stages(tmp_path, monkeypatch):
    path, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(path))
    extractor, segmenter = _ConfiguredText(), _ConfiguredParagraphs()
    arguments.update(extractor=extractor, segmenter=segmenter)
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(
        extractor=extractor, segmenter=segmenter,
        processor_ids=arguments["plan"].stages.processor_ids,
    ))
    fetcher = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    fetches = []
    original = fetcher.fetch

    def observed(*args, **kwargs):
        fetches.append(args[0].candidate_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(fetcher, "fetch", observed)
    arguments["content_fetcher"] = fetcher
    return arguments, extractor, segmenter, fetches


def _retain(prepared, run_ref):
    composition = prepared._composition
    return ReleaseCommitService(
        plan_ref=composition.plan_ref, controls=composition.controls,
        records=composition.records, document_catalog=composition.catalog,
    ).retain_release(None, run_ref)


@pytest.mark.parametrize("changed", ["extractor", "segmenter"])
def test_configured_stage_changes_rebuild_and_matching_settings_reuse(stages, changed):
    arguments, extractor, segmenter, fetches = stages
    with prepare_local_run(**arguments) as initial:
        initial_task = next(initial.task_source(initial.handoff))
        reference = initial.run()
        base = _retain(initial, reference)
        recovered = prepare_local_run(**arguments, handoff_ref=initial.handoff_ref)
        assert recovered.run() == reference
    assert (len(fetches), extractor.calls, segmenter.calls) == (1, 1, 1)

    arguments["plan"] = _replan(arguments["plan"], base_release=base)
    with prepare_local_run(**arguments) as unchanged:
        assert unchanged.handoff.expected_task_count == 0
        unchanged.run()
    assert (len(fetches), extractor.calls, segmenter.calls) == (1, 1, 1)

    if changed == "extractor":
        extractor.variant = "changed"
    else:
        segmenter.include = False
    with pytest.raises(ProfileError, match="settings differ"):
        prepare_local_run(**arguments)
    previous = arguments["plan"]
    arguments["plan"] = _replan(previous, stages=stage_policy(
        extractor=extractor, segmenter=segmenter, processor_ids=previous.stages.processor_ids,
    ))
    assert arguments["plan"].plan_id != previous.plan_id
    with prepare_local_run(**arguments) as changed_run:
        task = next(changed_run.task_source(changed_run.handoff))
        assert task.input_store.store_id != initial_task.input_store.store_id
        entry = changed_run._composition.stores.load(task.input_store).entries[0]
        assert entry.change is ChangeKind.REPAIR
        assert entry.execution_mode is EntryExecutionMode.FULL
        changed_run.run()
        result = changed_run.execute_task(changed_run.handoff, task)
        entry = changed_run._composition.stores.load(result.output_store).entries[0]
        assert not entry.failures
        assert entry.representations[0].configuration_digest == extractor.configuration_digest
        assert len(entry.segments) == (1 if segmenter.include else 0)
    # Stage changes currently rebuild captures too; finer reuse is a separate task.
    assert (len(fetches), extractor.calls, segmenter.calls) == (2, 2, 2)


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("changed", ["extractor", "segmenter"])
def test_mutated_stage_refuses_before_new_or_sealed_task_work(stages, monkeypatch, completed, changed):
    arguments, extractor, segmenter, fetches = stages
    with prepare_local_run(**arguments) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        if completed:
            prepared.execute_task(prepared.handoff, task)
        if changed == "extractor":
            extractor.variant = "mutated"
        else:
            segmenter.include = False

        def unexpected(*args):
            raise AssertionError("configuration refusal must precede store loading and delivery")

        monkeypatch.setattr("docspec.runtime.execution.load_latest_store", unexpected)
        monkeypatch.setattr(prepared._composition.delivery, "deliver_store", unexpected)
        with pytest.raises(IntegrityError, match="settings differ"):
            prepared.execute_task(prepared.handoff, task)
        with pytest.raises(IntegrityError, match="settings differ"):
            prepared._composition.executor.execute_store(task.input_store)
    assert len(fetches) == int(completed)


def test_zero_task_run_rechecks_mutated_stage(stages):
    arguments, extractor, _, fetches = stages
    with prepare_local_run(**arguments) as initial:
        base = _retain(initial, initial.run())
    arguments["plan"] = _replan(arguments["plan"], base_release=base)
    with prepare_local_run(**arguments) as unchanged:
        assert unchanged.handoff.expected_task_count == 0
        extractor.variant = "mutated-after-preparation"
        with pytest.raises(IntegrityError, match="settings differ"):
            unchanged.run()
    assert len(fetches) == 1


def test_empty_segmentation_checkpoint_binds_selected_child_policy(stages):
    arguments, extractor, segmenter, _ = stages
    segmenter.include = False
    arguments["plan"] = _replan(arguments["plan"], stages=stage_policy(
        extractor=extractor, segmenter=segmenter, processor_ids=arguments["plan"].stages.processor_ids,
    ))
    with prepare_local_run(**arguments) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        result = prepared.execute_task(prepared.handoff, task)
        composition = prepared._composition
        entry = composition.stores.load(result.output_store).entries[0]
        assert not entry.segments
        receipts = []
        for ref in entry.stage_receipts:
            value = composition.controls.load(ref)
            if value["format"] == "docspec-segmentation-receipt":
                assert (value["segmenterId"], value["policyDigest"]) == (
                    segmenter.segmenter_id, segmenter.policy_digest,
                )
                value["policyDigest"] = identity_digest({"another": "valid-policy"})
                ref = composition.controls.put(
                    kind="segmentation-receipts", artifact_id=stable_urn("segmentation-receipt", value), value=value,
                )
            receipts.append(ref)
        forged = replace(entry, stage_receipts=tuple(receipts))
        with pytest.raises(IntegrityError, match="selected segmenter policy"):
            composition.executor._checkpoints.verify_terminal_entry(forged, arguments["plan"])


@pytest.mark.parametrize("kind", ["extractor", "segmenter"])
@pytest.mark.parametrize("field", ["identity", "digest"])
def test_wrong_selected_child_output_refuses_before_persisting_it(stages, monkeypatch, kind, field):
    arguments, extractor, segmenter, _ = stages
    stage = extractor if kind == "extractor" else segmenter
    selected = stage.selected_identity

    def incorrect(value):
        identifier, digest = selected(value)
        return ("tests.other-child/v1", digest) if field == "identity" else (identifier, identity_digest({}))

    monkeypatch.setattr(stage, "selected_identity", incorrect)
    with prepare_local_run(**arguments) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        result = prepared.execute_task(prepared.handoff, task)
        entry = prepared._composition.stores.load(result.output_store).entries[0]
        assert entry.failures[-1].failure_class is FailureClass.ARTIFACT_INTEGRITY
        assert not entry.segments
        if kind == "extractor":
            assert not entry.representations
