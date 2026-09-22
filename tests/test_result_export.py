"""Selected Core export contract: an export is self-contained and reopens without the workspace or producer,
preserving exact selected values and blob bytes while excluding unrelated rows.

Also pins export pin/producer identity checks, the output-byte bound, read-only ledger behavior, refusal of a
mutated or pre-existing destination without publishing or replacing it, additional physical roots retaining
their exact record, and input streams closing on failure.
"""

from contextlib import closing, contextmanager
from dataclasses import replace
import sqlite3

import pytest
from rulespec_artifacts import ArtifactPin, Producer

from docspec.adapters.result_export.writer import export_result
from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.domain import core
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, LimitExceededError, StateTransitionError
from docspec.result_export import open_result_export
from docspec.runtime.core import CoreWorkspace

MAX_BYTES = 8 * 1024**2
PRODUCER = Producer("docspec", "git+https://example.test/docspec@" + "1" * 40,
    "urn:docspec:verifier:core-export", "1", "git+https://example.test/docspec@" + "1" * 40)


@pytest.fixture
def retained(tmp_path):
    """A workspace holding a selected state with one retained document plus unrelated states and inline secrets."""
    workspace = CoreWorkspace(tmp_path / "workspace")
    try:
        with workspace.publisher.session() as session:
            content = session.retain_bytes([b"retained document\n"])
            first = core.Entity(format_version=1, entity_id="document", entity_type="occurrence", value=content)
            unused = core.Entity(format_version=1, entity_id="unused", entity_type="occurrence", value=core.InlineValue(value={"secret": "not part of selected membership"}))
            workspace.states.create(session, state_id="selected", representation_id="original", unit_id="import",
                entities=(first, unused), members=(core.Membership(member_key="document", occurrence_id="document"),))
        workspace.create("unrelated", [("private", {"secret": "another dataset"})])
        yield workspace, BlobRef(content.locator, content.digest, content.byte_size, content.media_type)
    finally:
        workspace.close()


def export(workspace, destination, **kwargs):
    return export_result(workspace.publisher, workspace.records, "selected", destination,
                         producer=PRODUCER, max_output_bytes=MAX_BYTES, **kwargs)


def opened(destination, pin, **kwargs):
    return open_result_export(destination, expected_pin=pin, producer=PRODUCER,
                              **({"max_output_bytes": MAX_BYTES} | kwargs))


def test_export_opens_without_workspace_and_excludes_unrelated_rows(retained, tmp_path):
    workspace, reference = retained
    expected = list(workspace.rows("selected"))
    destination = tmp_path / "export"
    pin = export(workspace, destination)
    assert export(workspace, destination) == pin
    workspace.close()
    workspace.path.rename(tmp_path / "unavailable-workspace")
    with opened(destination, pin) as view:
        assert list(view.rows()) == expected
        assert view.read_blob(reference, max_bytes=1024) == b"retained document\n"
        assert view.record("state", "unrelated") is None
        assert view.record("entity", "unused") is None
        assert view.pin == pin and view.summary["stateId"] == "selected"
        with pytest.raises(StateTransitionError, match="read-only"):
            view._ledger.commit(__import__("docspec.ports.core_ledger", fromlist=["MetadataBatch"]).MetadataBatch("forbidden"))
        with sqlite3.connect(f"file:{destination}/ledger.sqlite?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT count(*) FROM heads").fetchone() == (0,)
    with pytest.raises(RuntimeError, match="closed"):
        list(view.rows())
    assert not list(destination.glob("ledger.sqlite-*"))
    for path in (destination / "records").rglob("*.parquet"):
        import pyarrow.parquet as parquet
        payloads = parquet.read_table(path, columns=["record_json"]).column(0).to_pylist()
        assert all(b"not part of selected membership" not in payload and b"another dataset" not in payload for payload in payloads)


def test_zero_work_successor_exports_complete_retained_population(retained, tmp_path):
    workspace, _ = retained
    # Selecting an existing state performs no producer work or new row writes.
    workspace.maintenance.select_current("choose", "documents", ("state", "selected"), None)
    before = list(workspace.rows("selected"))
    pin = export(workspace, tmp_path / "successor")
    with opened(tmp_path / "successor", pin) as view:
        assert list(view.rows()) == before


def test_export_repacks_artifacts_without_inline_values_or_unselected_rows(retained, tmp_path):
    workspace, _ = retained
    artifact = core.Entity(format_version=1, entity_id="prepared", entity_type="artifact",
                           value=core.InlineValue(value={"label": "selected metadata", "items": [1, 2]}))
    private = core.Entity(format_version=1, entity_id="private-artifact", entity_type="artifact",
                          value=core.InlineValue(value={"secret": "unselected artifact bytes"}))
    workspace.retain((artifact, private), unit_id="artifacts",
                     roots=(("entity", "prepared"), ("entity", "private-artifact")))
    destination = tmp_path / "artifact-export"
    pin = export(workspace, destination, additional_roots=[("entity", "prepared")])
    workspace.close()
    workspace.path.rename(tmp_path / "unavailable-workspace")
    with opened(destination, pin) as view:
        assert view.record("entity", "prepared") == artifact
        assert view.record("entity", "private-artifact") is None
        assert list(view.rows())[0][0] == "document"
    with sqlite3.connect(f"file:{destination}/ledger.sqlite?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM records WHERE kind='entity' AND payload IS NOT NULL").fetchone() == (0,)
        assert connection.execute("SELECT source_layer FROM records WHERE record_id='prepared'").fetchone()[0]
    import pyarrow.parquet as parquet
    payloads = [payload for path in (destination / "records").rglob("*.parquet")
                for payload in parquet.read_table(path, columns=["record_json"]).column(0).to_pylist()]
    assert any(b"selected metadata" in payload for payload in payloads)
    assert all(b"unselected artifact bytes" not in payload for payload in payloads)


def test_reader_checks_pin_producer_total_bound_and_exact_reference(retained, tmp_path):
    workspace, reference = retained
    destination = tmp_path / "export"
    pin = export(workspace, destination)
    with pytest.raises(IntegrityError):
        opened(destination, ArtifactPin(pin.logical_id, "sha256:" + "0" * 64))
    with pytest.raises(IntegrityError, match="producer"):
        open_result_export(destination, expected_pin=pin, producer=replace(PRODUCER, verifier_version="other"), max_output_bytes=MAX_BYTES)
    with pytest.raises(LimitExceededError, match="max_output_bytes"):
        opened(destination, pin, max_output_bytes=10)
    with opened(destination, pin) as view:
        with pytest.raises(LimitExceededError, match="max_bytes"):
            view.read_blob(reference, max_bytes=1)
        with pytest.raises(IntegrityError, match="does not belong"):
            view.read_blob(replace(reference, media_type="application/other"), max_bytes=1024)
        (destination / "blobs" / reference.locator).write_bytes(b"x" * reference.byte_size)
        with pytest.raises(IntegrityError, match="descriptor"):
            view.read_blob(reference, max_bytes=1024)


def test_abandoned_content_reader_closes_member(retained, tmp_path, monkeypatch):
    workspace, reference = retained
    destination = tmp_path / "export"
    pin = export(workspace, destination)
    with opened(destination, pin) as view:
        streams = []
        real_open = view._source.open
        @contextmanager
        def observed(key):
            with real_open(key) as stream:
                streams.append(stream)
                yield stream
        monkeypatch.setattr(view._source, "open", observed)
        values = view._publisher.blobs.read(reference, chunk_size=1)
        assert next(values) == b"r" and not streams[-1].closed
        values.close()
        assert streams[-1].closed
        rows = view.rows()
        next(rows)
        rows.close()


def test_interrupted_export_never_publishes_or_replaces_destination(retained, tmp_path, monkeypatch):
    workspace, _ = retained
    original = LocalContentAddressedBlobStore.read
    def interrupted(self, reference, **kwargs):
        with closing(original(self, reference, **kwargs)) as chunks:
            yield next(chunks)
            raise OSError("injected export interruption")
    monkeypatch.setattr(LocalContentAddressedBlobStore, "read", interrupted)
    destination = tmp_path / "incomplete"
    with pytest.raises(OSError, match="interruption"):
        export(workspace, destination)
    assert not destination.exists() and not list(tmp_path.glob(".incomplete.export-*"))
    monkeypatch.setattr(LocalContentAddressedBlobStore, "read", original)
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("existing different data")
    with pytest.raises(IntegrityError):
        export(workspace, destination)
    assert marker.read_text() == "existing different data"


@pytest.mark.parametrize("mutation", ["extra", "missing", "changed"])
def test_shared_container_refuses_mutated_members(retained, tmp_path, mutation):
    workspace, _ = retained
    destination = tmp_path / "export"
    pin = export(workspace, destination)
    if mutation == "extra":
        (destination / "unlisted.txt").write_text("extra")
    elif mutation == "missing":
        (destination / "ledger.sqlite").unlink()
    else:
        path = destination / "references.jsonl"
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(IntegrityError):
        opened(destination, pin)


def test_explicit_physical_representation_root_preserves_its_exact_record(retained, tmp_path):
    workspace, _ = retained
    original = next(workspace.ledger.read_records([("state_representation", "original")]))[0].value
    destination = tmp_path / "physical-export"
    pin = export(workspace, destination, additional_roots=[("state_representation", "original")])
    with opened(destination, pin) as view:
        assert view.record("state_representation", "original") == original
        assert ("state_representation", "original") in set(view.roots())


def test_failed_root_validation_closes_additional_root_stream(retained, tmp_path):
    workspace, _ = retained
    closed = []
    def roots():
        try:
            yield ("state", "missing")
            raise IntegrityError("source stopped")
        finally:
            closed.append(True)
    with pytest.raises(IntegrityError):
        export(workspace, tmp_path / "bad-roots", additional_roots=roots())
    assert closed == [True]
    assert not (tmp_path / "bad-roots").exists()
