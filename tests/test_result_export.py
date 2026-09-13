"""Normal retained results become bounded independently readable datasets."""

from contextlib import closing, contextmanager
from dataclasses import replace

import pytest
from rulespec_artifacts import ArtifactPin

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.result_export import ExportAdmissionError
from tests.support.experiments import _FailingProcessor, experiment as _experiment_fixture
from tests.support.processors import _CountingProcessor, _description
from tests.support.exports import export_run as _export_fixture, export_result as _export, open_export as _open

experiment = _experiment_fixture
export_run = _export_fixture



def test_export_opens_without_workspace_and_does_not_repeat_processing(export_run, tmp_path, monkeypatch):
    finish, retry, fetches, extractor, segmenter = export_run
    processor = _CountingProcessor(_description("export", "1", retry))
    release, local, _, settings = finish(processors=(processor,))
    expected = {layer.layer_kind: tuple(local.records(layer.layer_kind)) for layer in local._layers}
    before = len(fetches), extractor.calls, segmenter.calls, len(processor.calls)
    destination = tmp_path / "export"
    pin = _export(settings, release, destination, admission="nonempty-text")
    assert before == (len(fetches), extractor.calls, segmenter.calls, len(processor.calls))
    assert _export(settings, release, destination, admission="nonempty-text") == pin

    def no_workspace(*args, **kwargs):
        raise AssertionError("an independent reader must not open a workspace")

    monkeypatch.setattr("docspec.runtime.storage._local_storage", no_workspace)
    # Move every source dataset root; the export uses only embedded relative paths.
    for name, root in settings["workspace"].roots.items():
        if root.is_dir() and name != "reconciliation":
            root.rename(root.with_name(root.name + "-unavailable"))
    with _open(destination, pin, settings["export_producer"]) as view:
        assert view.pin == pin
        assert {kind: tuple(view.records(kind)) for kind in view.layer_kinds} == expected
        assert view.summary["counts"]["selectedItems"] == 1
        assert view.summary["counts"]["itemsWithNonemptyText"] == 1
        assert view.summary["semanticCompleteness"] == "not-established"
        captured = CapturedFile.from_dict(expected["files"][0]["payload"])
        representation = Representation.from_dict(expected["representations"][0]["payload"])
        assert view.read_blob(captured.blob, max_bytes=1024) == view.read_blob(representation.blob, max_bytes=1024)
        for row in expected["segments"]:
            segment = Segment.from_dict(row["payload"])
            content = view.read_blob(segment.content, max_bytes=1024)
            assert content == view.read_blob(representation.blob, max_bytes=1024)[
                segment.representation_start:segment.representation_end]
        for row in expected["receipts"]:
            reference = ArtifactRef.from_dict(row["payload"]["artifact"])
            value = view.read_evidence(reference)
            if value["format"] == "docspec-processor-invocation-receipt":
                assert view.read_evidence(ArtifactRef.from_dict(value["request"]["plan"]))["planId"] == local.plan.plan_id
                assert view.read_evidence(ArtifactRef.from_dict(value["result"]))["resultId"]
    with pytest.raises(RuntimeError, match="closed"):
        tuple(view.records("files"))


@pytest.mark.parametrize("mode", ["capture", "failure"])
def test_explicit_admission_preserves_valid_gaps_or_refuses_with_reasons(export_run, tmp_path, mode):
    finish, retry, *_ = export_run
    if mode == "capture":
        release, local, _, settings = finish(stop_after="capture", extractor=None, segmenter=None)
    else:
        failed = _FailingProcessor(_description("failing", "1", retry))
        release, local, _, settings = finish(processors=(failed,))
    pin = _export(settings, release, tmp_path / "evidence")
    with _open(tmp_path / "evidence", pin, settings["export_producer"]) as view:
        assert tuple(view.records("dispositions")) == tuple(local.records("dispositions"))
        assert view.summary["counts"]["failedItems"] == int(mode == "failure")
    with pytest.raises(ExportAdmissionError) as refusal:
        _export(settings, release, tmp_path / "text", admission="nonempty-text")
    assert refusal.value.report["reasonCounts"] == {
        "no-nonempty-text" if mode == "capture" else "terminal-failure": 1,
    }
    assert len(refusal.value.report["sample"]) == 1
    assert not (tmp_path / "text").exists()
    assert not list(tmp_path.glob(".text.export-*"))


def test_zero_task_successor_exports_entire_retained_population(export_run, tmp_path):
    finish, retry, *_ = export_run
    processor = _CountingProcessor(_description("retained", "1", retry))
    base, original, _, _ = finish(processors=(processor,))
    release, successor, entries, settings = finish(processors=(processor,), base_release=base)
    assert entries == ()
    pin = _export(settings, release, tmp_path / "unchanged", admission="nonempty-text")
    with _open(tmp_path / "unchanged", pin, settings["export_producer"]) as view:
        assert view.summary["counts"]["selectedItems"] == 1
        assert tuple(view.records("receipts")) == tuple(original.records("receipts"))
        assert successor.plan.plan_id != original.plan.plan_id
        assert tuple(view.records("source-items")) == tuple(original.records("source-items"))


def test_reader_checks_pin_producer_total_bound_and_exact_reference(export_run, tmp_path):
    finish, *_ = export_run
    release, _, _, settings = finish()
    destination = tmp_path / "export"
    pin = _export(settings, release, destination)
    with pytest.raises(IntegrityError):
        _open(destination, ArtifactPin(pin.logical_id, "sha256:" + "0" * 64), settings["export_producer"])
    with pytest.raises(IntegrityError, match="producer"):
        _open(destination, pin, replace(settings["export_producer"], verifier_version="other"))
    with pytest.raises(LimitExceededError, match="max_output_bytes"):
        _open(destination, pin, settings["export_producer"], max_output_bytes=10)
    with _open(destination, pin, settings["export_producer"]) as view:
        file = CapturedFile.from_dict(next(view.records("files"))["payload"])
        with pytest.raises(LimitExceededError, match="max_bytes"):
            view.read_blob(file.blob, max_bytes=1)
        with pytest.raises(IntegrityError, match="does not belong"):
            view.read_blob(replace(file.blob, media_type="application/not-the-admitted-reference"), max_bytes=1024)
        (destination / file.blob.locator).write_bytes(b"x" * file.blob.byte_size)
        with pytest.raises(IntegrityError, match="descriptor"):
            view.read_blob(file.blob, max_bytes=1024)


def test_abandoned_rows_release_the_member_file(export_run, tmp_path, monkeypatch):
    finish, *_ = export_run
    release, _, _, settings = finish()
    destination = tmp_path / "export"
    pin = _export(settings, release, destination)
    with _open(destination, pin, settings["export_producer"]) as view:
        opened = []
        real_open = view._source.open

        @contextmanager
        def observed(key):
            with real_open(key) as stream:
                opened.append(stream)
                yield stream

        monkeypatch.setattr(view._source, "open", observed)
        values = view.records("segments")
        next(values)
        assert not opened[-1].closed
        values.close()
        assert opened[-1].closed


def test_interrupted_export_never_publishes_or_replaces_a_destination(export_run, tmp_path, monkeypatch):
    finish, *_ = export_run
    release, _, _, settings = finish()
    original_read = LocalContentAddressedBlobStore.read

    def interrupted(self, reference, **kwargs):
        with closing(original_read(self, reference, **kwargs)) as chunks:
            yield next(chunks)
            raise OSError("injected export interruption")

    monkeypatch.setattr(LocalContentAddressedBlobStore, "read", interrupted)
    destination = tmp_path / "incomplete"
    with pytest.raises(OSError, match="interruption"):
        _export(settings, release, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".incomplete.export-*"))
    monkeypatch.setattr(LocalContentAddressedBlobStore, "read", original_read)
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("existing different data")
    with pytest.raises(IntegrityError):
        _export(settings, release, destination)
    assert marker.read_text() == "existing different data"


@pytest.mark.parametrize("mutation", ["extra", "missing", "changed"])
def test_shared_container_refuses_mutated_members(export_run, tmp_path, mutation):
    finish, *_ = export_run
    release, _, _, settings = finish()
    destination = tmp_path / "export"
    pin = _export(settings, release, destination)
    if mutation == "extra":
        (destination / "unlisted.txt").write_text("extra")
    else:
        member = next(destination.glob("control/**/*.json"))
        if mutation == "missing":
            member.unlink()
        else:
            member.write_bytes(member.read_bytes() + b" ")
    with pytest.raises(IntegrityError):
        _open(destination, pin, settings["export_producer"])
