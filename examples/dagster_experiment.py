"""Inject DocSpec services with native Dagster resources and run two small phases.

First run examples.offline_demo. Then:
python -m examples.dagster_experiment --reference /path/to/example --output /new/path
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import dagster
from rulespec_artifacts import Producer

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.dagster import DAGSTER_JOB_NAME, build_dagster_definitions
from docspec.domain.execution import StoreTaskResult
from docspec.domain.identity import canonical_json_file_bytes, identity_digest, sha256_digest
from docspec.domain.plans import ProcessingPlan, WorkLimits
from docspec.domain.policies import RetryPolicy
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef
from docspec.processing import ParagraphSegmenter, TextExtractor
from docspec.runtime import open_local_inspection, prepare_local_experiment
from docspec.workspace import LocalWorkspace
from examples.offline_demo import COMPLETED_AT, _phrase_values
from examples.phrase_match_processor import PhraseMatchProcessor


def _read(path: Path):
    return json.loads(path.read_bytes())


def _producers(reference: Path) -> tuple[Producer, Producer]:
    # Explicit acceptance for the known offline example, independently of the
    # source artifact's producer declaration.
    implementation = "urn:docspec:example:implementation:" + identity_digest(_read(reference / "implementation.json"))
    source = Producer("docspec-example", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)
    return source, replace(source, verifier_id="urn:docspec:verifier:document-release")


@dagster.resource(config_schema={"path": str})
def reference_root(context):
    return Path(context.resource_config["path"])


@dagster.resource(config_schema={"path": str}, required_resource_keys={"reference_root"})
def workspace(context):
    reference = LocalWorkspace(context.resources.reference_root)
    return LocalWorkspace(Path(context.resource_config["path"]), {
        "sourceCatalog": reference.roots["sourceCatalog"], "sourceContent": reference.roots["sourceContent"],
    })


@dagster.resource(required_resource_keys={"workspace"})
def fetcher(context):
    return LocalFileContentFetcher(context.resources.workspace.roots["sourceContent"])


@dagster.resource
def extractor():
    return TextExtractor()


@dagster.resource
def segmenter():
    return ParagraphSegmenter()


@dagster.resource(required_resource_keys={"reference_root"})
def processor(context):
    reference = context.resources.reference_root
    raw = (reference / "reference-inputs/vocabulary-v2.json").read_bytes()
    description, = ProcessingPlan.from_dict(_read(reference / "resource-v2.json")["plan"]).processors.processors
    pin, = description.external_resources
    selected = PhraseMatchProcessor(pin, raw, retry_policy=RetryPolicy(max_attempts=1, base_delay_milliseconds=0))
    if selected.description != description:
        raise ValueError("the saved reference processor differs from this example's implementation")
    return selected


REFERENCE_CONFIG = {
    "handoff": dagster.Field(dagster.Noneable(dagster.Permissive()), default_value=None),
    "base_release": dagster.Field(dagster.Noneable(dagster.Permissive()), default_value=None),
}


def _prepare(context, **stages):
    reference = context.resources.reference_root
    preview = _read(reference / "catalog-preview.json")
    source_producer, release_producer = _producers(reference)
    selected = [row["sourceItemId"] for row in preview["items"] if row["documentId"] in {"privacy", "security"}]
    if len(selected) != 2:
        raise ValueError("this example requires the two reference privacy/security documents")
    config = context.resource_config
    return prepare_local_experiment(
        SourceCatalogRef.from_dict(preview["reference"]), context.resources.workspace,
        limits=WorkLimits(1, 64 * 1024, 16, 16, 16, 1024 * 1024, 60, 1),
        selection={"includeItemIds": selected},
        source_catalog_producer=source_producer, document_release_producer=release_producer,
        completed_at=COMPLETED_AT, deadline_epoch_seconds=4_000_000_000,
        content_fetcher=context.resources.fetcher,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_milliseconds=0),
        base_release=None if config["base_release"] is None else DocumentReleaseRef.from_dict(config["base_release"]),
        handoff_ref=None if config["handoff"] is None else ArtifactRef.from_dict(config["handoff"]),
        **stages,
    )


@dagster.resource(config_schema=REFERENCE_CONFIG, required_resource_keys={"reference_root", "workspace", "fetcher"})
def capture_runtime(context):
    with _prepare(context, stop_after="capture") as prepared:
        yield prepared


@dagster.resource(
    config_schema=REFERENCE_CONFIG,
    required_resource_keys={"reference_root", "workspace", "fetcher", "extractor", "segmenter", "processor"},
)
def processing_runtime(context):
    with _prepare(context, extractor=context.resources.extractor, segmenter=context.resources.segmenter,
                  processors=(context.resources.processor,)) as prepared:
        yield prepared


CAPTURE_RESOURCES = {
    "reference_root": reference_root, "workspace": workspace, "fetcher": fetcher, "docspec_runtime": capture_runtime,
}
PROCESSING_RESOURCES = CAPTURE_RESOURCES | {
    "docspec_runtime": processing_runtime, "extractor": extractor, "segmenter": segmenter, "processor": processor,
}


def capture_job():
    return build_dagster_definitions(resource_defs=CAPTURE_RESOURCES).get_job_def(DAGSTER_JOB_NAME)


def processing_job():
    return build_dagster_definitions(
        resource_defs=PROCESSING_RESOURCES, retry_policy=dagster.RetryPolicy(max_retries=1),
    ).get_job_def(DAGSTER_JOB_NAME)


def run_example(reference: Path, output: Path) -> dict:
    reference, output = reference.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "dagster").mkdir()
    phases, base = {}, None
    with dagster.DagsterInstance.local_temp(str(output / "dagster")) as instance:
        for phase, definitions, job_factory in (
            ("capture", CAPTURE_RESOURCES, capture_job), ("processing", PROCESSING_RESOURCES, processing_job),
        ):
            config = {
                "reference_root": {"config": {"path": str(reference)}},
                "workspace": {"config": {"path": str(output / "dataset")}},
                "docspec_runtime": {"config": {"base_release": None if base is None else base.to_dict()}},
            }
            # Dagster's own DI works outside an op as well. Preparation saves
            # existing DocSpec references; the native worker reconstructs them.
            with dagster.build_resources(definitions, resource_config=config, instance=instance) as resources:
                prepared = resources.docspec_runtime
                assert prepared.handoff.expected_task_count == 2
                config["docspec_runtime"]["config"]["handoff"] = prepared.handoff_ref.to_dict()
            with dagster.execute_job(
                dagster.reconstructable(job_factory), instance=instance,
                run_config={"resources": config, "execution": {"config": {"max_concurrent": 2}}},
            ) as result:
                if not result.success:
                    raise RuntimeError(f"inspect native Dagster run {result.run_id}; the phase did not complete")
                # This fixture has exactly two messages. Large deployments
                # should consume task results through their chosen native IO.
                results = tuple(StoreTaskResult.from_bytes(raw) for raw in result.output_for_node("execute_store_task").values())
                native_run = result.run_id
            with dagster.build_resources(definitions, resource_config=config, instance=instance) as resources:
                prepared = resources.docspec_runtime
                base = prepared.retain(prepared.reconcile(results))
                view = open_local_inspection(prepared.plan, resources.workspace,
                    document_release_producer=_producers(reference)[1], release_ref=base)
                phases[phase] = {"dagsterRunId": native_run, "handoff": prepared.handoff_ref.to_dict(),
                                 "release": base.to_dict(), "inspection": view.summary()}
                if phase == "processing":
                    actual = _phrase_values(view, resources.processor)
                    source_digests = {sha256_digest((resources.workspace.roots["sourceContent"] / f"{name}.txt").read_bytes())
                                      for name in ("privacy", "security")}
                    expected = [value for value in _read(reference / "matches.json")["resource-v2"]
                                if value["enclosingSourceEvidence"]["sourceDigest"] in source_digests]
                    assert actual == expected
                    assert phases[phase]["inspection"]["work"]["counts"]["newCapturedFiles"] == 0
    summary = {"verdict": "pass", "tasksPerPhase": 2, "cleanOutputValuesAgree": True, "phases": phases}
    (output / "dagster-example.json").write_bytes(canonical_json_file_bytes(summary))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run_example(args.reference, args.output)
    print(json.dumps({key: value for key, value in summary.items() if key != "phases"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
