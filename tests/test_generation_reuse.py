"""Metadata-first retries reuse only the local owner's completed admission."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from rulespec_artifacts import canonical_json_bytes

from docspec.adapters import generation_source, generation_validator
from docspec.application import generation_admission
from docspec.application.core_publication import Publication
from docspec.domain.table_rows import KeySpelling
from docspec.errors import IntegrityError, StaleBaseError
from docspec.runtime import CoreWorkspace
from tests.support.generations import generation, publication, serve_https

FAMILY, TABLE = "federal-register", "federal_register"


def admit(workspace, source):
    return workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")


def observe_source(monkeypatch):
    reads = []
    reader = generation_source._reader

    @contextmanager
    def observed(source):
        with reader(source) as (chunks, remote):
            def counted(key, limit):
                reads.append(key)
                yield from chunks(key, limit)
            yield counted, remote

    monkeypatch.setattr(generation_source, "_reader", observed)
    return reads


def test_repeat_reopens_owner_proof_without_source_payload_and_rehashes_retained_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        assert "federal_register.parquet" in reads  # instrument's positive control
    reads.clear()
    with CoreWorkspace(tmp_path / "workspace", create=False) as workspace:
        verified = []
        verify = workspace.records.verify_members
        def counted(reference):
            verified.append(reference)
            return verify(reference)
        monkeypatch.setattr(workspace.records, "verify_members", counted)
        assert admit(workspace, source) == first
        assert reads == ["artifact.json", "members.json"]
        assert verified  # zero source transfer still renews local byte integrity


def test_changed_current_publication_pin_reads_the_new_payload(tmp_path, monkeypatch):
    base, first_source, second_source = tmp_path / "publication", tmp_path / "first", tmp_path / "second"
    pin, member, description = generation(first_source)
    publication(base, first_source, pin, member, description, prefix="generations/first")
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, base)
        reads.clear()
        changed = pa.table({"document_number": ["2026-1"], "publication_date": ["2026-09-25"], "title": ["Changed"]})
        pin, member, description = generation(second_source, data=changed)
        publication(base, second_source, pin, member, description, prefix="generations/second")
        second = admit(workspace, base)
        assert second.state_id != first.state_id
        assert "generations/second/federal_register.parquet" in reads
        assert workspace.ledger.current("fr") == ("state", second.state_id)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_retained_payload_refuses_before_source_staging(tmp_path, monkeypatch, damage):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admit(workspace, source)
        path = next(workspace.records.root.rglob("*.parquet"))
        if damage == "missing":
            path.unlink()
        else:
            payload = path.read_bytes()
            path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        reads = observe_source(monkeypatch)
        with pytest.raises(IntegrityError):
            admit(workspace, source)
        assert not any(key.endswith(".parquet") for key in reads)


def test_corrupt_admission_metadata_refuses(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        with sqlite3.connect(workspace.path / "ledger.sqlite") as connection:
            connection.execute("UPDATE records SET payload=?, source_layer=NULL WHERE kind='entity' AND record_id=?",
                               (b"{}", first.state_id + ":report"))
        reads = observe_source(monkeypatch)
        with pytest.raises(IntegrityError):
            admit(workspace, source)
        assert not any(key.endswith(".parquet") for key in reads)


def test_validator_mismatch_requires_a_new_workspace_without_source_payload(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        monkeypatch.setattr(generation_validator, "validator_identity", lambda table: "sha256:" + "0" * 64)
        reads = observe_source(monkeypatch)
        for _ in range(2):
            reads.clear()
            with pytest.raises(IntegrityError, match="new workspace destination"):
                admit(workspace, source)
            assert not any(key.endswith(".parquet") for key in reads)
            assert workspace.ledger.current("fr") == ("state", first.state_id)


@pytest.mark.parametrize("label", ["root", "members", "etl-receipts-0"])
def test_corrupt_retained_admission_input_refuses_before_source_payload(tmp_path, monkeypatch, label):
    source = Path(__file__).parent / "fixtures/generations/native-receipts"
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = workspace.admit_generation(source, family="fixture-native", table="fixture_native")
        request = workspace.generating_request(first.state_id)
        entity_id = next(item.entity_id for item in request.inputs if item.label == label)
        with workspace.publisher.session() as session:
            content = next(session.read_records([("entity", entity_id)]))[0].value.value
        path = workspace.blobs.root / content.locator
        payload = path.read_bytes()
        path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        reads = observe_source(monkeypatch)
        with pytest.raises(IntegrityError):
            workspace.admit_generation(source, family="fixture-native", table="fixture_native")
        assert not any(key.endswith(".parquet") for key in reads)


def test_missing_owner_completion_refuses_before_source_payload(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        with sqlite3.connect(workspace.path / "ledger.sqlite") as connection:
            connection.execute("DELETE FROM links WHERE owner_kind='result' AND owner_id=? AND label='output:state'",
                               (first.state_id + ":execution:result",))
        reads = observe_source(monkeypatch)
        with pytest.raises(IntegrityError, match="completed owner admission"):
            admit(workspace, source)
        assert not any(key.endswith(".parquet") for key in reads)


def test_identity_policy_change_does_not_reuse_prior_state(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        monkeypatch.setattr(generation_source, "_key_rule", lambda *args: KeySpelling("value", "1", ("document_number",)))
        reads = observe_source(monkeypatch)
        with pytest.raises(IntegrityError, match="key spelling|identity"):
            admit(workspace, source)
        assert "federal_register.parquet" in reads
        assert workspace.ledger.current("fr") == ("state", first.state_id)


def test_same_pin_with_changed_publication_selection_refuses(tmp_path, monkeypatch):
    source, base = tmp_path / "source", tmp_path / "publication"
    pin, member, description = generation(source)
    pointer = publication(base, source, pin, member, description)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admit(workspace, base)
        pointer["families"][FAMILY]["tables"][member.object_key]["byteSize"] += 1
        (base / "publication.v2.json").write_bytes(canonical_json_bytes(pointer))
        reads = observe_source(monkeypatch)
        with pytest.raises(IntegrityError, match="publication table descriptor"):
            admit(workspace, base)
        assert not any(key.endswith(".parquet") for key in reads)


@pytest.mark.parametrize("damage", ["missing-table-member", "external-member"])
def test_malformed_publication_members_refuse_without_payload_staging(tmp_path, monkeypatch, damage):
    source, base = tmp_path / "source", tmp_path / "publication"
    pin, member, description = generation(source)
    publication(base, source, pin, member, description)
    pointer = json.loads((base / "publication.v2.json").read_bytes())
    manifest_path = base / pointer["families"][FAMILY]["prefix"] / "members.json"
    manifest = json.loads(manifest_path.read_bytes())
    if damage == "missing-table-member":
        manifest["members"] = []
    else:
        descriptor = manifest["members"][0]
        del descriptor["objectKey"], descriptor["sha256"]
        descriptor["blobRef"] = "urn:fixture:external-member"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace, pytest.raises(IntegrityError):
        admit(workspace, base)
    assert not any(key.endswith(".parquet") for key in reads)


@pytest.mark.parametrize("bad", [None, [], 1])
def test_malformed_sibling_description_refuses_before_payload(tmp_path, monkeypatch, bad):
    source, base = tmp_path / "source", tmp_path / "publication"
    pin, member, description = generation(source)
    pointer = publication(base, source, pin, member, description)
    target = base / pointer["families"][FAMILY]["prefix"]
    root = json.loads((target / "artifact.json").read_bytes())
    root["spec"]["tables"]["sibling.parquet"] = bad
    (target / "artifact.json").write_bytes(canonical_json_bytes(root))
    manifest = json.loads((target / "members.json").read_bytes())
    manifest["members"].append({**manifest["members"][0], "objectKey": "sibling.parquet"})
    (target / "members.json").write_bytes(canonical_json_bytes(manifest))
    pointer["families"][FAMILY]["tables"]["sibling.parquet"] = dict(
        pointer["families"][FAMILY]["tables"][member.object_key])
    (base / "publication.v2.json").write_bytes(canonical_json_bytes(pointer))
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace, pytest.raises(IntegrityError, match="must be an object"):
        admit(workspace, base)
    assert not any(key.endswith(".parquet") for key in reads)


@pytest.mark.parametrize("bad", [[], 1])
def test_nonobject_receipt_declaration_refuses_before_payload(tmp_path, monkeypatch, bad):
    source = tmp_path / "source"
    generation(source)
    root = json.loads((source / "artifact.json").read_bytes())
    root["spec"]["etlReceipts"] = bad
    (source / "artifact.json").write_bytes(canonical_json_bytes(root))
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace, pytest.raises(IntegrityError, match="must be an object"):
        admit(workspace, source)
    assert not any(key.endswith(".parquet") for key in reads)


def test_validator_identity_observes_source_bytes_and_rejects_symlink(tmp_path, monkeypatch, request):
    request.addfinalizer(generation_validator._owner_sources.cache_clear)  # later tests must not see the fake tree
    root = tmp_path / "owner"
    root.mkdir()
    source = root / "__init__.py"
    source.write_text("# before\n")
    monkeypatch.setattr(generation_validator, "import_module", lambda name: SimpleNamespace(__file__=str(source)))
    generation_validator._owner_sources.cache_clear()
    before = generation_validator.validator_identity()
    source.write_text("# after\n")
    assert generation_validator.validator_identity() == before  # hashed once per process: it binds what was imported
    generation_validator._owner_sources.cache_clear()
    assert generation_validator.validator_identity() != before
    (root / "helper.py").symlink_to(source)
    generation_validator._owner_sources.cache_clear()
    with pytest.raises(IntegrityError, match="nonsymlink"):
        generation_validator.validator_identity()
    with pytest.raises(IntegrityError, match="nonsymlink"):  # a refusal is not cached
        generation_validator.validator_identity()


def test_unsupported_retained_report_requires_a_new_workspace_without_source_payload(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with monkeypatch.context() as patch:  # an admission report written by the retired version-1 format
        patch.setattr(generation_admission, "REPORT_VERSION", 1)
        with CoreWorkspace(tmp_path / "workspace") as workspace:
            first = admit(workspace, source)
    assert first.report["version"] == 1
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace", create=False) as workspace:
        for _ in range(2):  # refuses every time; never restages or upgrades
            reads.clear()
            with pytest.raises(IntegrityError, match="new workspace destination"):
                admit(workspace, source)
            assert not any(key.endswith(".parquet") for key in reads)
        assert workspace.ledger.current("fr") == ("state", first.state_id)
        with workspace.publisher.session() as session:
            report = next(session.read_records([("entity", first.state_id + ":report")]))[0].value.value.value
        assert report["version"] == 1  # historical evidence unchanged
    with CoreWorkspace(tmp_path / "fresh") as fresh:
        again = admit(fresh, source)
    assert again.state_id == first.state_id and again.report["version"] == 2 and again.report["validator"]


def test_concurrent_admission_requires_an_explicit_retry(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        files = sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file())
        reads = observe_source(monkeypatch)
        with monkeypatch.context() as patch:  # another admission commits after this one's retained lookup
            patch.setattr(generation_admission, "retained_generation", lambda *args, **kwargs: None)
            with pytest.raises(StaleBaseError, match="retry"):
                admit(workspace, source)
        assert "federal_register.parquet" in reads  # the losing attempt did stage
        assert sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file()) == files
        assert not list(workspace.records.staging_directory.iterdir())
        assert workspace.ledger.current("fr") == ("state", first.state_id)
        reads.clear()
        assert admit(workspace, source) == first  # the explicit retry reuses the committed admission
        assert not any(key.endswith(".parquet") for key in reads)


@pytest.mark.parametrize("transport", ["local", "https"])
def test_publication_repeat_reads_only_pointer_and_pinned_metadata(tmp_path, monkeypatch, transport):
    source, base = tmp_path / "source", tmp_path / "publication"
    pin, member, description = generation(source)
    prefix = publication(base, source, pin, member, description)["families"][FAMILY]["prefix"]
    location = base
    if transport == "https":
        serve_https(monkeypatch, base)
        location = "https://example.test/data"
    reads = observe_source(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, location)
        assert f"{prefix}/{member.object_key}" in reads  # positive control
        reads.clear()
        assert admit(workspace, location) == first
    assert reads == ["publication.v2.json", f"{prefix}/artifact.json", f"{prefix}/members.json"]


def hide_published_once(patch):
    """Both admission checks run before a competitor commits: the retained lookup finds nothing and the first state check misses."""
    real, checks = generation_admission._published, []

    def hidden_once(session, state_id):
        checks.append(state_id)
        return False if len(checks) == 1 else real(session, state_id)

    patch.setattr(generation_admission, "retained_generation", lambda *args, **kwargs: None)
    patch.setattr(generation_admission, "_published", hidden_once)
    return checks


def test_publish_collision_requires_an_explicit_retry(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit(workspace, source)
        with monkeypatch.context() as patch:
            checks = hide_published_once(patch)
            with pytest.raises(StaleBaseError, match="retry"):  # the publish collides, not a verdict on the evidence
                admit(workspace, source)
        assert len(checks) == 2  # the state is rechecked only after the failed publish
        assert workspace.ledger.current("fr") == ("state", first.state_id)
        reads = observe_source(monkeypatch)
        assert admit(workspace, source) == first  # the explicit retry reuses the committed admission
        assert not any(key.endswith(".parquet") for key in reads)


def test_refusal_before_publishing_stays_a_refusal_when_the_state_exists_afterwards(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)

    def refuse(*args, **kwargs):
        raise IntegrityError("evidence refused")

    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admit(workspace, source)  # the competitor's committed admission
        with monkeypatch.context() as patch:
            checks = hide_published_once(patch)
            patch.setattr(generation_admission, "_unit", refuse)
            with pytest.raises(IntegrityError, match="evidence refused") as caught:
                admit(workspace, source)
        assert not isinstance(caught.value, StaleBaseError)  # a retry loop must not hide a genuine refusal
        assert len(checks) == 1  # nothing was published, so no collision recheck ran


def test_publish_failure_without_a_competitor_stays_a_refusal(tmp_path, monkeypatch):
    source = tmp_path / "source"
    generation(source)

    def refuse(*args, **kwargs):
        raise IntegrityError("publish refused")

    monkeypatch.setattr(Publication, "publish", refuse)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        with pytest.raises(IntegrityError, match="publish refused") as caught:
            admit(workspace, source)
        assert not isinstance(caught.value, StaleBaseError)  # no state exists afterwards, so it is no collision
        assert workspace.ledger.current("fr") is None
