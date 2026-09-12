"""Requested stages form one pinned prefix; processor reruns do not change it."""

from dataclasses import replace

import pytest

from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.jobs import ChangeKind, DocumentEntry, EntryExecutionMode
from docspec.domain.plans import StagePolicy
from docspec.errors import ProfileError
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.runtime import stage_policy
SOURCE = SourceItem("item", "v1", (CandidateFile("primary", "source.txt", "text/plain"),))


@pytest.mark.parametrize("stop_after,extraction,segmentation", [
    ("capture", False, False),
    ("extraction", True, False),
    ("segmentation", True, True),
    ("processing", True, True),
])
def test_requested_stage_prefix_round_trips(stop_after, extraction, segmentation):
    stages = stage_policy(stop_after=stop_after)
    assert stages.requests_extraction is extraction
    assert stages.requests_segmentation is segmentation
    assert (stages.extractor_configuration_digest is not None) is extraction
    assert (stages.segmenter_policy_digest is not None) is segmentation
    assert StagePolicy.from_dict(stages.to_dict()) == stages


@pytest.mark.parametrize("field", [
    "extractor_id", "extractor_configuration_digest", "segmenter_id", "segmenter_policy_digest",
])
def test_stage_pin_pairs_cannot_be_partial(field):
    with pytest.raises(ValueError, match="present together"):
        replace(stage_policy(), **{field: None})


def test_stages_require_their_preceding_stage():
    with pytest.raises(ValueError, match="segmentation requires extraction"):
        replace(stage_policy(), extractor_id=None, extractor_configuration_digest=None)
    with pytest.raises(ValueError, match="processors require extraction and segmentation"):
        replace(stage_policy(stop_after="extraction"), processor_ids=("test.processor",))


@pytest.mark.parametrize("stop_after,arguments", [
    ("capture", {"extractor": TextExtractor()}),
    ("capture", {"segmenter": ParagraphSegmenter()}),
    ("extraction", {"segmenter": ParagraphSegmenter()}),
])
def test_unused_explicit_stage_objects_are_refused(stop_after, arguments):
    with pytest.raises(ProfileError, match="not requested"):
        stage_policy(stop_after=stop_after, **arguments)


@pytest.mark.parametrize("stop_after", ["capture", "extraction", "segmentation"])
def test_processor_ids_conflict_with_earlier_stopping_points(stop_after):
    with pytest.raises(ValueError, match="require stop_after"):
        stage_policy(stop_after=stop_after, processor_ids=("test.processor",))


def test_unknown_stopping_point_is_refused():
    with pytest.raises(ValueError, match="stop_after must be"):
        stage_policy(stop_after="unknown")


def test_processor_subset_preserves_the_complete_requested_stages():
    stages = stage_policy(processor_ids=("processor.one", "processor.two", "processor.three"))
    full = DocumentEntry.create(SOURCE, ChangeKind.REPAIR, stages)
    reused = DocumentEntry.create(
        SOURCE, ChangeKind.REPAIR, stages,
        execution_mode=EntryExecutionMode.FROM_SEGMENTS,
        processor_ids_to_run=("processor.two",),
    )
    assert full.processor_ids_to_run == stages.processor_ids
    assert reused.requested_stages == full.requested_stages == stages
    assert reused.processor_ids_to_run == ("processor.two",)
    assert reused.entry_id != full.entry_id
    assert DocumentEntry.from_dict(reused.to_dict()) == reused


@pytest.mark.parametrize("processors", [
    ("processor.two", "processor.one"), ("processor.one", "processor.one"), ("unknown",),
])
def test_processor_reruns_require_an_ordered_subset(processors):
    with pytest.raises(ValueError, match="ordered subset"):
        DocumentEntry.create(
            SOURCE, ChangeKind.REPAIR,
            stage_policy(processor_ids=("processor.one", "processor.two")),
            execution_mode=EntryExecutionMode.FROM_SEGMENTS,
            processor_ids_to_run=processors,
        )


@pytest.mark.parametrize("mode", [
    EntryExecutionMode.FULL, EntryExecutionMode.FROM_CAPTURES, EntryExecutionMode.FROM_REPRESENTATIONS,
])
def test_running_before_segments_requires_the_complete_processor_graph(mode):
    with pytest.raises(ValueError, match="full processor graph"):
        DocumentEntry.create(
            SOURCE, ChangeKind.REPAIR,
            stage_policy(processor_ids=("processor.one", "processor.two")),
            execution_mode=mode, processor_ids_to_run=("processor.two",),
        )


def test_saved_entries_require_explicit_processor_rerun_fields():
    entry = DocumentEntry.create(SOURCE, ChangeKind.ADDED, stage_policy())
    value = entry.to_dict()
    with pytest.raises(ValueError, match="closed shape"):
        DocumentEntry.from_dict({key: item for key, item in value.items() if key != "processorIdsToRun"})
    with pytest.raises(ValueError, match="must be an array"):
        DocumentEntry.from_dict(value | {"processorIdsToRun": "processor.one"})
