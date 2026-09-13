"""Self-consistent containers must still satisfy bounded DocSpec evidence."""

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from rulespec_artifacts import (
    ArtifactPin, LocalMemberSource, Producer, admit_artifact, build_artifact_root,
    describe_member, iter_member_descriptors, stamp_root, write_member_manifest,
)

from docspec.adapters.result_export import admission
from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.controls import LocalJsonControlRepository
from docspec.domain.content import CandidateFile, Segment, SourceItem
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, stable_urn
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import export_local_result, prepare_local_experiment
from docspec.workspace import LocalWorkspace
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.support.profiles import _seeded_local_run_arguments
from tests.support.experiments import _FailingProcessor, experiment as _experiment_fixture
from tests.support.exports import export_run as _export_fixture, export_result, open_export
from tests.support.processors import _CountingProcessor, _description

experiment = _experiment_fixture
export_run = _export_fixture


def _replace_rows(path, index, kind, values):
    key = next(layer["objectKey"] for layer in index["layers"] if layer["kind"] == kind)
    (path / key).write_bytes(b"".join(canonical_json_file_bytes(value) for value in sorted(values, key=lambda row: row["recordId"])))


def _reseal(path, artifact, descriptors):
    """Use the independent shared container writer around deliberately bad semantics."""
    source = LocalMemberSource(path)
    members = tuple(describe_member(source, object_key=value.object_key, role=value.role,
        media_type=value.media_type, record_count=value.record_count, schema_id=value.schema_id)
        for value in sorted(descriptors, key=lambda value: value.object_key))
    with (path / "members.json").open("wb") as stream:
        manifest = write_member_manifest(stream, scope_kind="global", scope_id="active-result",
            object_key="members.json", members=members)
    root = build_artifact_root(kind=artifact.root["kind"], spec=artifact.root["spec"],
        producer=Producer.from_dict(artifact.root["producer"], path="producer"),
        inputs=artifact.inputs, manifests=(manifest,))
    (path / "artifact.json").write_bytes(canonical_json_bytes(root))
    return ArtifactPin(root["logicalId"], root["artifactDigest"])


def test_understated_root_cannot_bypass_payload_read_bound(export_run, tmp_path, monkeypatch):
    finish, *_ = export_run
    release, _, _, settings = finish()
    path = tmp_path / "export"
    export_result(settings, release, path)
    root = json.loads((path / "artifact.json").read_bytes())
    root["counts"]["totalMemberByteSize"] = 0
    root = stamp_root(root)
    payload = canonical_json_bytes(root)
    (path / "artifact.json").write_bytes(payload)
    pin = ArtifactPin(root["logicalId"], root["artifactDigest"])
    bound = len(payload) + sum(value["byteSize"] for value in root["memberManifests"]) + 1
    opened = []
    actual_open = LocalMemberSource.open

    @contextmanager
    def observed(self, key):
        opened.append(key)
        with actual_open(self, key) as stream:
            yield stream

    monkeypatch.setattr(LocalMemberSource, "open", observed)
    with pytest.raises(LimitExceededError, match="max_output_bytes"):
        open_export(path, pin, settings["export_producer"], max_output_bytes=bound)
    assert opened == ["artifact.json"]


def test_self_consistent_wrong_segment_bytes_refuse_the_claimed_slice(export_run, tmp_path):
    finish, *_ = export_run
    release, local, _, settings = finish()
    path = tmp_path / "export"
    export_result(settings, release, path)
    source = LocalMemberSource(path)
    artifact = admit_artifact(source)
    descriptors = list(iter_member_descriptors(artifact, source))
    index = json.loads((path / "export.json").read_bytes())
    segments = list(local.records("segments"))
    original = Segment.from_dict(segments[0]["payload"])
    wrong_bytes = b"X" * original.content.byte_size
    wrong_blob = LocalContentAddressedBlobStore(path).put_if_absent(
        (wrong_bytes,), media_type=original.content.media_type,
    )
    changed = Segment.create(
        source_item_id=original.source_item_id, file_id=original.file_id,
        representation_id=original.representation_id, representation_start=original.representation_start,
        representation_end=original.representation_end, ordinal=original.ordinal, kind=original.kind,
        content=wrong_blob, evidence=original.evidence, segmenter_id=original.segmenter_id,
        policy_digest=original.policy_digest, derivation=original.derivation,
    )
    segments[0] = {**segments[0], "recordId": changed.segment_id, "payload": changed.to_dict()}
    _replace_rows(path, index, "segments", segments)
    receipts = list(local.records("receipts"))
    controls = LocalJsonControlRepository(path, create=False)
    for ordinal, row in enumerate(receipts):
        reference = ArtifactRef.from_dict(row["payload"]["artifact"])
        value = controls.load(reference)
        if value["format"] == "docspec-segmentation-receipt":
            value["segments"] = [changed.segment_id if identifier == original.segment_id else identifier
                for identifier in value["segments"]]
            replacement = controls.put(kind="segmentation-receipt",
                artifact_id=stable_urn("segmentation-receipt", value), value=value)
            receipts[ordinal] = {**row, "recordId": replacement.artifact_id,
                "payload": {**row["payload"], "artifact": replacement.to_dict()}}
            descriptors.append(describe_member(LocalMemberSource(path), object_key=replacement.locator,
                role="evidence", media_type="application/json", schema_id="urn:docspec:control-artifact:1.0"))
    _replace_rows(path, index, "receipts", receipts)
    descriptors.append(describe_member(LocalMemberSource(path), object_key=wrong_blob.locator,
        role="blob", media_type="application/octet-stream"))
    changed_pin = _reseal(path, artifact, descriptors)
    # Common membership, byte digests and all replaced IDs/receipt links agree.
    assert admit_artifact(LocalMemberSource(path), expected_pin=changed_pin).pin == changed_pin
    with pytest.raises(IntegrityError, match="exact representation slice"):
        open_export(path, changed_pin, settings["export_producer"])


def test_item_bound_includes_large_processor_results_before_loading_them(export_run, tmp_path, monkeypatch):
    finish, retry, *_ = export_run

    class VerboseProcessor(_CountingProcessor):
        def process(self, *args):
            return replace(super().process(*args), warnings=("provider explanation " * 4000,))

    processor = VerboseProcessor(_description("verbose", "1", retry))
    release, _, _, settings = finish(processors=(processor,))
    path = tmp_path / "export"
    pin = export_result(settings, release, path)
    # The actual named bound is reduced for this fixture. Small row/attempt
    # metadata fits; the separately referenced result bodies do not.
    monkeypatch.setattr(admission, "ITEM_BYTES", 64 * 1024)
    with pytest.raises(LimitExceededError, match="item controls"):
        open_export(path, pin, settings["export_producer"])


def test_mixed_failed_item_uses_its_original_processor_plan(export_run, tmp_path):
    finish, retry, *_ = export_run
    upstream = _CountingProcessor(_description("upstream", "1", retry))
    failed = _FailingProcessor(_description("failed", "1", retry, dependencies=(upstream.description.processor_id,)))
    independent = _CountingProcessor(_description("independent", "1", retry))
    base, prior, _, _ = finish(processors=(upstream, failed, independent))
    changed = _CountingProcessor(_description("independent", "2", retry))
    release, current, entries, settings = finish(processors=(upstream, failed, changed), base_release=base)
    assert entries == ()
    assert current.plan.stages != prior.plan.stages
    pin = export_result(settings, release, tmp_path / "mixed")
    with open_export(tmp_path / "mixed", pin, settings["export_producer"]) as view:
        assert tuple(view.records("dispositions")) == tuple(prior.records("dispositions"))
        assert view.summary["counts"]["failedItems"] == 1
        assert tuple(view.records(f"derived:{upstream.description.processor_id}")) == tuple(
            prior.records(f"derived:{upstream.description.processor_id}"))


def test_one_export_contains_processor_evidence_from_two_owning_plans(tmp_path):
    arguments = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    original = arguments["workspace"]
    catalog_root = tmp_path / "two-items"
    source = write_shared_source_catalog(catalog_root, tuple(SourceItem(name, "v1", (
        CandidateFile("primary", "document.txt", "text/plain", transport_version="fixture:v1"),
    ), metadata={"expectedSegments": 1}) for name in ("document-a", "document-b")))
    workspace = LocalWorkspace(original.root, original.roots | {"sourceCatalog": catalog_root})
    processor = _CountingProcessor(_description("mixed-plans", "1", arguments["retry_policy"]))
    settings = {
        "limits": arguments["plan"].limits, "retry_policy": arguments["retry_policy"],
        "accepted_failure_policy": arguments["accepted_failure_policy"],
        "source_catalog_producer": arguments["source_catalog_producer"],
        "document_release_producer": arguments["document_release_producer"],
        "completed_at": arguments["completed_at"], "deadline_epoch_seconds": arguments["deadline_epoch_seconds"],
        "content_fetcher": SharedFixtureContentFetcher(workspace.roots["sourceContent"]),
        "processors": (processor,),
    }
    base = None
    plans = []
    for identifier in ("document-a", "document-b"):
        with prepare_local_experiment(source, workspace, **settings, base_release=base,
            selection={"includeItemIds": [identifier]}) as prepared:
            base = prepared.retain(prepared.run())
            plan = prepared.plan
            plans.append(plan.plan_id)
    export_producer = replace(arguments["document_release_producer"], verifier_id="urn:docspec:verifier:result-export")
    path = tmp_path / "mixed-plans"
    pin = export_local_result(plan, workspace, base, path, admission="nonempty-text",
        document_release_producer=arguments["document_release_producer"], export_producer=export_producer,
        max_output_bytes=8 * 1024**2)
    with open_export(path, pin, export_producer) as view:
        owning_plans = set()
        for row in view.records("receipts"):
            value = view.read_evidence(ArtifactRef.from_dict(row["payload"]["artifact"]))
            if value["format"] == "docspec-processor-invocation-receipt":
                owning_plans.add(value["request"]["plan"]["artifactId"])
        assert owning_plans == set(plans)
        assert len(owning_plans) == 2
        assert view.summary["counts"]["selectedItems"] == 2


def test_same_plan_different_results_have_distinct_export_identities(export_run, tmp_path):
    finish, retry, *_ = export_run
    processor = _FailingProcessor(_description("outcome", "1", retry), error=TimeoutError)
    failed, first, _, first_settings = finish(processors=(processor,))
    processor.fail = False
    successful, second, _, second_settings = finish(processors=(processor,), fresh=True)
    assert first.plan.plan_id == second.plan.plan_id
    assert failed.digest != successful.digest
    first_pin = export_result(first_settings, failed, tmp_path / "failed")
    second_pin = export_result(second_settings, successful, tmp_path / "successful")
    assert first_pin.logical_id != second_pin.logical_id
