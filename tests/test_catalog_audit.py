"""Metadata browsing and complete retained-state audit have distinct costs and claims."""

import json
from dataclasses import fields

import pytest

from docspec.cli import main
from docspec.domain.content import CapturedFile, Segment
from docspec.domain.identity import canonical_json_file_bytes, identity_digest
from docspec.domain.plans import ProcessingPlan
from docspec.domain.receipts import RunReceipt
from docspec.errors import IntegrityError
from docspec.processing import ParagraphSegmenter, TextExtractor
from docspec.processing.artifacts import build_segment
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import export_local_result, open_local_inspection, prepare_local_run, stage_policy
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run_arguments


class _AlternateMediaSegmenter(ParagraphSegmenter):
    segmenter_id = "tests.alternate-segment-media/v1"
    policy_digest = identity_digest({"mediaType": "application/octet-stream"})

    def segment(self, representation):
        return (build_segment(representation, ordinal=0, kind="paragraph", start=0,
            end=len(representation.content), segmenter_id=self.segmenter_id,
            policy_digest=self.policy_digest, derivation=("whole-text",),
            media_type="application/octet-stream"),)


@pytest.fixture
def prepared(tmp_path, request):
    arguments = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    if getattr(request, "param", None) == "alternate-media":
        arguments.update(extractor=TextExtractor(), segmenter=_AlternateMediaSegmenter())
        plan = arguments["plan"]
        values = {field.name: getattr(plan, field.name) for field in fields(plan) if field.name != "plan_id"}
        arguments["plan"] = ProcessingPlan.create(**(values | {"stages": stage_policy(
            extractor=arguments["extractor"], segmenter=arguments["segmenter"],
            processor_ids=plan.stages.processor_ids,
        )}))
    with prepare_local_run(**arguments) as local:
        yield arguments, local, local.run()


@pytest.fixture
def retained(prepared):
    arguments, local, run = prepared
    return arguments, local, run, local.retain(run)


def test_metadata_open_does_not_read_dataset_members(retained, monkeypatch):
    _, local, _, reference = retained
    composition = local._composition
    catalog = composition.catalog
    expected = catalog.open(reference)
    catalog.select(reference, expected_current=None)

    def no_data(*args, **kwargs):
        pytest.fail("metadata admission read retained data")

    for service, methods in (
        (composition.records, ("verify", "stream", "partition_policy")),
        (composition.stores, ("load", "planned_store_ledger")),
        (catalog.verifier._blobs, ("verify", "read")),
    ):
        for method in methods:
            monkeypatch.setattr(service, method, no_data)
    assert catalog.open(reference) == expected
    assert catalog.open_reader(reference).release == expected
    assert catalog.current() == reference


@pytest.mark.parametrize("control", ["processing_plan", "run_receipt", "catalog_commit_receipt", "execution_handoff"])
def test_metadata_open_still_refuses_changed_linked_controls(retained, control):
    _, local, run_ref, reference = retained
    composition = local._composition
    release = composition.catalog.open(reference)
    owner = RunReceipt.from_dict(composition.controls.load(run_ref)) if control == "execution_handoff" else release
    path = composition.controls.root / getattr(owner, control).locator
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(IntegrityError):
        composition.catalog.open(reference)


@pytest.mark.parametrize("operation", ["audit", "stage", "retain", "select", "inspection", "export"])
def test_unused_missing_blob_is_browsable_but_complete_operations_refuse(retained, tmp_path, operation):
    arguments, local, run, reference = retained
    composition = local._composition
    catalog = composition.catalog
    release = catalog.open(reference)
    reader = catalog.open_reader(reference)
    captured = CapturedFile.from_dict(next(reader.scan(layer_kind="files"))["payload"])
    (arguments["workspace"].roots["blobStorage"] / captured.blob.locator).unlink()

    assert catalog.open(reference) == release
    assert catalog.open_reader(reference).release == release
    # Reading a files row verifies the record member; reading its content checks the blob.
    assert next(catalog.scan(reference, layer_kind="files"))["payload"] == captured.to_dict()
    with pytest.raises(IntegrityError):
        b"".join(catalog.verifier._blobs.read(captured.blob))
    with pytest.raises(IntegrityError):
        if operation == "audit":
            catalog.audit(reference)
        elif operation == "stage":
            catalog.stage(release)
        elif operation == "retain":
            local.retain(run)
        elif operation == "select":
            catalog.select(reference, expected_current=None)
        elif operation == "inspection":
            open_local_inspection(arguments["plan"], arguments["workspace"],
                document_release_producer=arguments["document_release_producer"], release_ref=reference)
        else:
            export_local_result(arguments["plan"], arguments["workspace"], reference, tmp_path / "export",
                admission="retained-evidence", document_release_producer=arguments["document_release_producer"],
                export_producer=arguments["document_release_producer"], max_output_bytes=10_000_000)
    assert catalog.current() is None
    assert not (tmp_path / "export").exists()


def test_corrupt_record_member_is_refused_on_use_and_audit(retained):
    _, local, _, reference = retained
    composition = local._composition
    reader = composition.catalog.open_reader(reference)
    layer = next(layer for layer in reader.release.active_layers if layer.layer_kind == "files")
    state = json.loads((composition.records.root / layer.state_ref).read_bytes())
    member = composition.records.root / state["members"][0]["path"]
    member.write_bytes(member.read_bytes() + b" ")
    assert composition.catalog.open(reference) == reader.release
    with pytest.raises(IntegrityError):
        list(reader.scan(layer_kind="files"))
    with pytest.raises(IntegrityError):
        composition.catalog.audit(reference)


def test_publication_audits_each_boundary_once_and_rechecks_existing_destination(prepared, monkeypatch):
    _, local, run = prepared
    catalog = local._composition.catalog
    original = catalog.verifier.verify
    audits = []

    def observed(release):
        audits.append(release.release_id)
        return original(release)

    monkeypatch.setattr(catalog.verifier, "verify", observed)
    reference = local.retain(run)
    # The builder audits before sealing; retain audits the staged input in its own call.
    assert len(audits) == 2
    assert catalog.open(reference).release_id == reference.release_id
    assert len(audits) == 2
    assert local.retain(run) == reference
    # A separate existing published destination is audited before discarding the new stage.
    assert len(audits) == 5
    catalog.select(reference, expected_current=None)
    assert len(audits) == 6


@pytest.mark.parametrize("prepared", ["alternate-media"], indirect=True)
def test_complete_audit_preserves_media_sensitive_blob_verification(retained, monkeypatch):
    _, local, _, reference = retained
    catalog = local._composition.catalog
    reader = catalog.open_reader(reference)
    captured = CapturedFile.from_dict(next(reader.scan(layer_kind="files"))["payload"])
    segment = Segment.from_dict(next(reader.scan(layer_kind="segments"))["payload"])
    assert (captured.blob.locator, captured.blob.digest, captured.blob.byte_size) == (
        segment.content.locator, segment.content.digest, segment.content.byte_size)
    assert captured.blob.media_type != segment.content.media_type
    blobs = catalog.verifier._blobs
    original = blobs.verify
    visited = []

    def verify_stored_media(blob):
        original(blob)
        visited.append(blob)
        # Model the existing S3 adapter's stored ContentType agreement rule.
        if blob.media_type != captured.blob.media_type:
            raise IntegrityError("stored ContentType differs from reference")

    monkeypatch.setattr(blobs, "verify", verify_stored_media)
    with pytest.raises(IntegrityError, match="ContentType"):
        catalog.audit(reference)
    assert visited == [captured.blob, segment.content]


def test_cli_open_labels_metadata_and_audit_requires_complete_bytes(retained, tmp_path, capfd):
    arguments, local, _, reference = retained
    roots = arguments["workspace"].roots
    producer = arguments["document_release_producer"]
    path = tmp_path / "release-reference.json"
    path.write_bytes(canonical_json_file_bytes(reference.to_dict()))
    options = ["--reference", str(path), "--implementation-id", producer.implementation_id,
        "--verifier-implementation-id", producer.verifier_implementation_id]
    for flag, key in (("catalog", "documentCatalog"), ("blob", "blobStorage"),
        ("record", "recordStorage"), ("store", "documentStores"), ("control", "controlRepository")):
        options.extend([f"--{flag}-root", str(roots[key])])
    assert main(["document-catalog", "audit", *options]) == 0
    assert json.loads(capfd.readouterr().out)["verificationScope"] == "complete-retained-state"
    captured = CapturedFile.from_dict(next(local._composition.catalog.scan(reference, layer_kind="files"))["payload"])
    (roots["blobStorage"] / captured.blob.locator).unlink()
    assert main(["document-catalog", "open", *options]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["verdict"] == "metadata-valid"
    assert result["verificationScope"] == "pinned-metadata-and-linked-controls"
    assert main(["document-catalog", "audit", *options]) == 2
    assert "blob" in capfd.readouterr().err
