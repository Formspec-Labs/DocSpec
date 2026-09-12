"""One checkpoint shares physical blob reads; every later admission checks anew."""

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest

from docspec.adapters.s3_blob import S3ContentAddressedBlobStore
from docspec.domain.jobs import StoreState
from docspec.errors import IntegrityError
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import prepare_local_run
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run_arguments


@pytest.fixture
def extraction_checkpoint(tmp_path, monkeypatch):
    arguments = _seeded_local_run_arguments(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])

    class WorkerStopped(BaseException):
        pass

    with prepare_local_run(**arguments) as prepared:
        task = next(prepared.task_source(prepared.handoff))
        stores = prepared._composition.stores
        save = stores.save

        def stop_after_extraction(store):
            reference = save(store)
            if store.state is StoreState.RUNNING and store.entries[0].representations:
                raise WorkerStopped()
            return reference

        with monkeypatch.context() as interrupted:
            interrupted.setattr(stores, "save", stop_after_extraction)
            with pytest.raises(WorkerStopped):
                prepared.execute_task(prepared.handoff, task)
        reference = stores.latest(task.input_store.store_id)
        assert reference is not None
        entry, = stores.load(reference).entries
        assert not entry.terminal and not entry.segments
        captured, = entry.captured_files
        representation, = entry.representations
        assert captured.blob == representation.blob
        yield prepared, task, reference, entry


def _observe_verification(prepared, monkeypatch):
    verifier = prepared._composition.executor._checkpoints
    verify = verifier._blobs.verify
    calls = []

    def observed(reference):
        calls.append(reference)
        return verify(reference)

    monkeypatch.setattr(verifier._blobs, "verify", observed)
    return verifier, calls


def test_identical_checkpoint_blobs_share_one_read_and_later_corruption_refuses_before_save(
    extraction_checkpoint, monkeypatch,
):
    prepared, task, saved, entry = extraction_checkpoint
    verifier, calls = _observe_verification(prepared, monkeypatch)
    blob = entry.captured_files[0].blob
    assert verifier.verify_entry(entry, prepared.plan).extraction_complete
    assert calls == [blob]
    calls.clear()

    path = prepared._composition.workspace.roots["blobStorage"] / blob.locator
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(IntegrityError, match="blob bytes differ"):
        prepared.execute_task(prepared.handoff, task)
    assert calls == [blob]
    assert prepared._composition.stores.latest(task.input_store.store_id) == saved


@pytest.mark.parametrize("field", ["locator", "byte_size"])
def test_same_digest_with_different_physical_reference_is_not_skipped(
    extraction_checkpoint, monkeypatch, field,
):
    prepared, _task, _saved, entry = extraction_checkpoint
    verifier, calls = _observe_verification(prepared, monkeypatch)
    captured = entry.captured_files[0].blob
    changed = replace(captured, **{
        field: captured.locator + ".other" if field == "locator" else captured.byte_size + 1,
    })
    representation = replace(entry.representations[0], blob=changed)
    forged = replace(entry, representations=(representation,))

    with pytest.raises(IntegrityError, match="locator|size"):
        verifier.verify_entry(forged, prepared.plan)
    assert calls == [captured, changed]


def test_same_bytes_with_different_media_type_still_checks_s3_metadata(
    extraction_checkpoint, monkeypatch,
):
    prepared, _task, _saved, entry = extraction_checkpoint
    blob = entry.captured_files[0].blob
    content = (prepared._composition.workspace.roots["blobStorage"] / blob.locator).read_bytes()
    body = BytesIO(content)
    head_requests = []

    def head(**request):
        assert request == {"Bucket": "checkpoint-fixture", "Key": blob.locator}
        head_requests.append(request)
        return {
            "ContentLength": len(content), "ContentType": blob.media_type,
            "Metadata": {"docspec-sha256": blob.digest, "docspec-byte-size": str(len(content))},
        }

    client = SimpleNamespace(
        head_object=head,
        get_object=lambda **_: {"ContentLength": len(content), "Body": body},
    )
    verifier = prepared._composition.executor._checkpoints
    monkeypatch.setattr(verifier, "_blobs", S3ContentAddressedBlobStore(client, bucket="checkpoint-fixture"))
    representation = replace(entry.representations[0], blob=replace(blob, media_type="application/octet-stream"))

    with pytest.raises(IntegrityError, match="S3 blob media type differs"):
        verifier.verify_entry(replace(entry, representations=(representation,)), prepared.plan)
    assert len(head_requests) == 2
    assert body.closed
