"""Small configurations derive the same pinned plans and runtime evidence."""

from dataclasses import fields, replace

import pytest

from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, ProcessorExecutionScope, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorDescription, ProcessorSet
from docspec.errors import IntegrityError, ProfileError
from docspec.processing.extraction import TextExtractor
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import local_execution_limits, open_local_inspection, prepare_local_experiment, prepare_local_run, stage_policy
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run
from tests.support.processors import _CountingProcessor, _description


@pytest.fixture
def configured(tmp_path):
    request, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    original = _local_run_arguments(_local_run_request(request))
    settings = {
        "limits": original["plan"].limits,
        "source_catalog_producer": original["source_catalog_producer"],
        "document_release_producer": original["document_release_producer"],
        "completed_at": original["completed_at"],
        "deadline_epoch_seconds": original["deadline_epoch_seconds"],
        "content_fetcher": SharedFixtureContentFetcher(original["workspace"].roots["sourceContent"]),
    }
    return original["plan"].source_catalog, original["workspace"], settings, original


def test_small_capture_then_processing_config_preserves_inputs_and_recovers(configured, monkeypatch):
    source, workspace, settings, _ = configured
    with prepare_local_experiment(source, workspace, stop_after="capture", **settings) as captured:
        base = captured.retain(captured.run())
        capture_plan = captured.plan
    assert capture_plan.base_release is None
    assert capture_plan.partition_count == 1
    assert capture_plan.stages.extractor_id is None

    def unexpected(*args, **kwargs):
        raise AssertionError("later processing refetched retained bytes")

    monkeypatch.setattr(settings["content_fetcher"], "fetch", unexpected)
    next_settings = settings | {
        "base_release": base, "extractor": TextExtractor(), "segmenter": ParagraphSegmenter(),
    }
    with prepare_local_experiment(source, workspace, **next_settings) as prepared:
        run = prepared.run()
        result = prepared.retain(run)
        with prepare_local_experiment(source, workspace, handoff_ref=prepared.handoff_ref, **next_settings) as recovered:
            assert recovered.plan == prepared.plan
            assert recovered.handoff_ref == prepared.handoff_ref
            assert recovered.run() == run
        view = open_local_inspection(prepared.plan, workspace,
                                     document_release_producer=settings["document_release_producer"], release_ref=result)
        assert view.summary()["work"]["counts"]["reusedCapturedFiles"] == 1
        assert view.summary()["result"]["layers"]["representations"] == 1
        assert prepared.plan.base_release == base


def test_derived_and_explicit_settings_have_identical_plan_handoff_and_run(configured):
    source, workspace, settings, _ = configured
    retry, accepted = RetryPolicy(max_attempts=settings["limits"].max_attempts), AcceptedFailurePolicy()
    extractor, segmenter = TextExtractor(), ParagraphSegmenter()
    processor = ContentStatisticsProcessor(retry_policy=retry)
    selected = {"extractor": extractor, "segmenter": segmenter, "processors": (processor,)}
    with prepare_local_experiment(source, workspace, **settings, **selected) as derived:
        plan = ProcessingPlan.create(
            source_catalog=source, base_release=None, profiles=ProfileRegistry.builtin().local_profiles(),
            limits=settings["limits"], stages=stage_policy(extractor=extractor, segmenter=segmenter,
                processor_ids=(processor.description.processor_id,)),
            processors=ProcessorSet((processor.description,)), partition_count=1, selection={},
            retention_policy=RetentionPolicy.retain_all(), data_use_policy=DataUsePolicy.local_content(),
            retry_policy_digest=retry.digest, accepted_failure_policy_digest=accepted.digest,
        )
        explicit = {key: value for key, value in settings.items() if key != "limits"}
        with prepare_local_run(plan, workspace, **explicit, retry_policy=retry, accepted_failure_policy=accepted,
                               execution_limits=local_execution_limits(), extractor=extractor, segmenter=segmenter,
                               processors={processor.description.processor_id: processor}) as prepared:
            assert derived.plan == prepared.plan == plan
            assert derived.handoff_ref == prepared.handoff_ref
            assert derived.execution_profile == prepared.execution_profile
            assert derived.run() == prepared.run()


def test_default_stage_objects_are_constructed_once_and_the_same_objects_execute(configured, monkeypatch):
    source, workspace, settings, _ = configured
    constructed = []
    extractor, segmenter = TextExtractor(), ParagraphSegmenter()

    def extraction():
        constructed.append("extractor")
        return extractor

    def segmentation():
        constructed.append("segmenter")
        return segmenter

    monkeypatch.setattr("docspec.runtime.composition.DefaultExtractorRegistry", extraction)
    monkeypatch.setattr("docspec.runtime.composition.DefaultSegmenterRegistry", segmentation)
    with prepare_local_experiment(source, workspace, **settings) as prepared:
        assert constructed == ["extractor", "segmenter"]
        assert prepared.plan.stages == stage_policy(extractor=extractor, segmenter=segmenter)
        assert prepared._composition.executor._extractor is extractor
        assert prepared._composition.executor._segmenter is segmenter
        prepared.run()
        assert constructed == ["extractor", "segmenter"]


def test_processor_dependency_order_is_derived_without_changing_implementation_bindings(configured):
    source, workspace, settings, _ = configured
    retry = RetryPolicy(max_attempts=settings["limits"].max_attempts)
    first = _CountingProcessor(_description("first", "1", retry))
    dependent = _CountingProcessor(_description("dependent", "1", retry, dependencies=(first.description.processor_id,)))
    selected = {"extractor": TextExtractor(), "segmenter": ParagraphSegmenter()}
    with prepare_local_experiment(source, workspace, **settings, **selected, processors=(dependent, first)) as reversed_run:
        with prepare_local_experiment(source, workspace, **settings, **selected, processors=(first, dependent)) as ordered_run:
            assert reversed_run.plan == ordered_run.plan
            assert reversed_run.handoff_ref == ordered_run.handoff_ref
            assert reversed_run.plan.stages.processor_ids == (first.description.processor_id, dependent.description.processor_id)
            run = reversed_run.run()
            assert ordered_run.run() == run
            result = reversed_run.retain(run)
            view = open_local_inspection(reversed_run.plan, workspace,
                document_release_producer=settings["document_release_producer"], release_ref=result)
            rows = tuple(view.records(f"derived:{dependent.description.processor_id}"))
            assert rows[0]["payload"]["value"]["processorName"] == "dependent"
            assert len(rows[0]["payload"]["inputIds"]) == 2
    assert len(first.calls) == len(dependent.calls) == 1


@pytest.mark.parametrize("mismatch", ["retry", "data-use", "external-execution"])
def test_processor_policy_mismatch_refuses_before_creating_dataset_state(configured, mismatch):
    source, workspace, settings, _ = configured
    retry = RetryPolicy(max_attempts=settings["limits"].max_attempts)
    processor = ContentStatisticsProcessor(retry_policy=retry)
    overrides = {}
    if mismatch == "retry":
        processor = ContentStatisticsProcessor(retry_policy=replace(retry, max_attempts=retry.max_attempts + 1))
    elif mismatch == "data-use":
        overrides["data_use_policy"] = DataUsePolicy.create(
            execution_scope=ProcessorExecutionScope.LOCAL_ONLY, allowed_fields=("content",),
        )
    else:
        values = {field.name: getattr(processor.description, field.name)
                  for field in fields(ProcessorDescription) if field.name != "processor_id"}
        processor.description = ProcessorDescription.create(
            **(values | {"execution_scope": ProcessorExecutionScope.DECLARED_EXTERNAL}),
        )
    before = {path.relative_to(workspace.root) for path in workspace.root.rglob("*")}
    with pytest.raises(ProfileError, match="data-use policy|retry policy"):
        prepare_local_experiment(source, workspace, **settings, **overrides, processors=(processor,))
    assert {path.relative_to(workspace.root) for path in workspace.root.rglob("*")} == before


@pytest.mark.parametrize("invalid", ["stop", "duplicate-processors", "unrequested-stage", "selection", "network-limit", "retry-policy"])
def test_invalid_configuration_refuses_before_creating_dataset_state(configured, invalid):
    source, workspace, settings, _ = configured
    before = {path.relative_to(workspace.root) for path in workspace.root.rglob("*")}
    extra = {}
    if invalid == "stop":
        extra["stop_after"] = "unsupported"
    elif invalid == "duplicate-processors":
        processor = ContentStatisticsProcessor()
        extra["processors"] = (processor, processor)
    elif invalid == "unrequested-stage":
        extra.update(stop_after="capture", extractor=TextExtractor())
    elif invalid == "selection":
        extra["selection"] = {"inventedFilter": True}
    elif invalid == "network-limit":
        extra["execution_limits"] = local_execution_limits(max_network_bytes_per_task=1)
    else:
        extra["retry_policy"] = RetryPolicy(max_attempts=settings["limits"].max_attempts + 1)
    with pytest.raises((ValueError, ProfileError, IntegrityError)):
        prepare_local_experiment(source, workspace, **settings, **extra)
    assert {path.relative_to(workspace.root) for path in workspace.root.rglob("*")} == before


def test_advanced_policy_profile_and_limit_overrides_are_pinned(configured):
    source, workspace, settings, _ = configured
    retention = RetentionPolicy.create(minimum_age_seconds=600)
    execution = local_execution_limits(worker_count=2, max_in_flight=3)
    with prepare_local_experiment(
        source, workspace, stop_after="capture", **settings,
        partition_count=3, profiles=ProfileRegistry.builtin().local_profiles(),
        retention_policy=retention, execution_limits=execution,
        selection={"includeItemIds": ["document-a"]},
    ) as prepared:
        assert prepared.plan.partition_count == 3
        assert prepared.plan.retention_policy == retention
        assert prepared.plan.selection == {"includeItemIds": ["document-a"]}
        assert prepared.execution_profile.limits == execution


def test_saved_handoff_refuses_different_explicit_time_and_stage_settings(configured):
    source, workspace, settings, _ = configured
    selected = {"extractor": TextExtractor(), "segmenter": ParagraphSegmenter()}
    with prepare_local_experiment(source, workspace, **settings, **selected) as prepared:
        for changes in (
            {"completed_at": "2026-09-12T00:00:00Z"},
            {"deadline_epoch_seconds": settings["deadline_epoch_seconds"] + 1},
            {"stop_after": "extraction", "segmenter": None},
        ):
            changed = settings | selected | changes
            with pytest.raises((IntegrityError, ProfileError)):
                prepare_local_experiment(source, workspace, handoff_ref=prepared.handoff_ref, **changed)


def test_source_acceptance_is_explicit_and_not_derived_from_input(configured):
    source, workspace, settings, _ = configured
    incomplete = {key: value for key, value in settings.items() if key != "source_catalog_producer"}
    with pytest.raises(TypeError, match="source_catalog_producer"):
        prepare_local_experiment(source, workspace, stop_after="capture", **incomplete)
    wrong = replace(settings["source_catalog_producer"], implementation_id="unaccepted-input")
    with pytest.raises(IntegrityError):
        prepare_local_experiment(source, workspace, stop_after="capture", **(settings | {"source_catalog_producer": wrong}))


def test_shared_execution_defaults_preserve_explicit_cli_behavior(configured):
    _, _, _, original = configured
    assert local_execution_limits() == original["execution_limits"]
    limits = local_execution_limits(worker_count=3)
    assert limits.max_in_flight == 3
    assert limits.max_concurrency_per_worker == 1
    with pytest.raises(ValueError):
        local_execution_limits(worker_count=0)
