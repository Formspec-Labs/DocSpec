"""Public processing uses cache storage only for processors that request it."""

from dataclasses import fields

from docspec.domain.policies import RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorCacheMode, ProcessorCachePolicy, ProcessorDescription
from docspec.processing import ParagraphSegmenter, TextExtractor
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import open_local_inspection, prepare_local_experiment
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run_arguments


def _configured(tmp_path):
    original = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    workspace = original["workspace"]
    settings = {
        "limits": original["plan"].limits,
        "source_catalog_producer": original["source_catalog_producer"],
        "document_release_producer": original["document_release_producer"],
        "completed_at": original["completed_at"],
        "deadline_epoch_seconds": original["deadline_epoch_seconds"],
        "content_fetcher": SharedFixtureContentFetcher(workspace.roots["sourceContent"]),
        "extractor": TextExtractor(),
        "segmenter": ParagraphSegmenter(),
    }
    return original["plan"].source_catalog, workspace, settings


def _disabled_processor(retry):
    processor = ContentStatisticsProcessor(retry_policy=retry)
    definition = {
        field.name: getattr(processor.description, field.name)
        for field in fields(ProcessorDescription)
        if field.name != "processor_id"
    }
    processor.description = ProcessorDescription.create(
        **(definition | {
            "name": "uncached-statistics",
            "cache_policy": ProcessorCachePolicy(ProcessorCacheMode.DISABLED, None),
        })
    )
    return processor


def test_cache_disabled_processor_runs_retains_and_recovers_without_cache_storage(tmp_path, monkeypatch):
    source, workspace, settings = _configured(tmp_path)
    processor = _disabled_processor(RetryPolicy(max_attempts=settings["limits"].max_attempts))

    def unexpected_cache(*args, **kwargs):
        raise AssertionError("cache-disabled processing constructed a processor cache")

    monkeypatch.setattr("docspec.runtime.composition.LocalSqliteProcessorResultCache", unexpected_cache)
    with prepare_local_experiment(source, workspace, **settings, processors=(processor,)) as prepared:
        run = prepared.run()
        release = prepared.retain(run)
        view = open_local_inspection(
            prepared.plan, workspace,
            document_release_producer=settings["document_release_producer"], release_ref=release,
        )
        rows = tuple(view.records(f"derived:{processor.description.processor_id}"))
        assert len(rows) == 1
        assert rows[0]["payload"]["value"]["wordCount"] == 3
        assert rows[0]["payload"]["value"]["byteCount"] == len(b"One conformance paragraph.")
        assert view.summary()["work"]["counts"]["recordedProcessorAttempts"] == 1
        with prepare_local_experiment(
            source, workspace, **settings, processors=(processor,), handoff_ref=prepared.handoff_ref,
        ) as recovered:
            assert recovered.run() == run
    assert not tuple(workspace.roots["reconciliation"].glob("processor-results.sqlite3*"))


def test_cache_enabled_processor_reuses_a_real_result_across_plan_changes(tmp_path, monkeypatch):
    source, workspace, settings = _configured(tmp_path)
    retry = RetryPolicy(max_attempts=settings["limits"].max_attempts)
    processor = ContentStatisticsProcessor(retry_policy=retry)
    disabled = _disabled_processor(retry)
    process, disabled_process = processor.process, disabled.process
    calls, disabled_calls = [], []

    def observed_process(*args, **kwargs):
        calls.append(args[0].request_id)
        return process(*args, **kwargs)

    def observed_disabled_process(*args, **kwargs):
        disabled_calls.append(args[0].request_id)
        return disabled_process(*args, **kwargs)

    monkeypatch.setattr(processor, "process", observed_process)
    monkeypatch.setattr(disabled, "process", observed_disabled_process)
    selected = (processor, disabled)
    with prepare_local_experiment(source, workspace, **settings, processors=selected) as first:
        base = first.retain(first.run())
        original_plan = first.plan
        original_view = open_local_inspection(
            first.plan, workspace,
            document_release_producer=settings["document_release_producer"], release_ref=base,
        )
        original_rows = {
            item.description.processor_id: tuple(original_view.records(f"derived:{item.description.processor_id}"))
            for item in selected
        }
    assert len(calls) == len(disabled_calls) == 1
    assert (workspace.roots["reconciliation"] / "processor-results.sqlite3").is_file()

    with prepare_local_experiment(
        source, workspace, **settings, processors=selected, base_release=base,
        retention_policy=RetentionPolicy.create(minimum_age_seconds=1),
    ) as second:
        result = second.retain(second.run())
        assert second.plan.plan_id != original_plan.plan_id
        view = open_local_inspection(
            second.plan, workspace,
            document_release_producer=settings["document_release_producer"], release_ref=result,
        )
        for item in selected:
            identifier = item.description.processor_id
            rows = tuple(view.records(f"derived:{identifier}"))
            assert len(rows) == len(original_rows[identifier]) == 1
            assert rows[0]["payload"]["value"] == original_rows[identifier][0]["payload"]["value"]
        assert view.summary()["work"]["counts"]["processorCacheHits"] == 1
    assert len(calls) == 1
    assert len(disabled_calls) == 2
