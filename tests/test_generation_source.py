"""Generation staging binds producer evidence without rewriting or trusting its pointer."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rulespec_artifacts import canonical_json_bytes, stamp_root

from docspec.adapters.content_fetchers.https import HttpsContentFetcher
from docspec.adapters.generation_source import stage_generation
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import KeySpelling
from docspec.errors import IntegrityError, LimitExceededError
from tests.support.generations import generation, publication


def test_local_generation_preserves_source_and_returns_exact_evidence(tmp_path):
    source, scratch = tmp_path / "source", tmp_path / "staging"
    scratch.mkdir()
    pin, member, _ = generation(source)
    originals = {path.name: path.read_bytes() for path in source.iterdir()}
    with stage_generation(source, family="federal-register", table="federal_register", directory=scratch, expected_pin=pin) as admitted:
        assert admitted.pin == pin and admitted.member == member
        assert admitted.path.read_bytes() == originals["federal_register.parquet"]
        assert admitted.root_bytes == originals["artifact.json"]
        assert admitted.manifest_bytes == originals["members.json"]
        assert admitted.record_count == 1
        assert admitted.columns == (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("title", "VARCHAR"))
        assert admitted.key == KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
        staged = admitted.path
    assert not staged.exists() and not list(scratch.iterdir())
    assert {path.name: path.read_bytes() for path in source.iterdir()} == originals


def test_local_publication_descriptor_and_explicit_pin_are_checked(tmp_path):
    source, base = tmp_path / "source", tmp_path / "published"
    pin, member, description = generation(source)
    pointer = publication(base, source, pin, member, description)
    with stage_generation(base, family="federal-register", table="federal_register") as admitted:
        assert admitted.pin == pin
    with pytest.raises(IntegrityError, match="expected generation pin"):
        with stage_generation(base, family="federal-register", table="federal_register", expected_pin=replace(pin, artifact_digest="sha256:" + "0" * 64)):
            pytest.fail("changed pin admitted")
    pointer["families"]["federal-register"]["tables"][member.object_key]["byteSize"] += 1
    (base / "publication.json").write_bytes(canonical_json_bytes(pointer))
    with pytest.raises(IntegrityError, match="publication table descriptor"):
        with stage_generation(base, family="federal-register", table="federal_register"):
            pytest.fail("changed pointer admitted")


@pytest.mark.parametrize("change,match", [
    ({"status": "local-partial"}, "complete-family"),
    ({"kind": "another-kind"}, "complete-family"),
    ({"rows": 2}, "footer row count"),
    ({"columns": [["wrong", "VARCHAR"]]}, "footer schema"),
    ({"identity": ["missing"]}, "identity fields"),
])
def test_sealed_but_semantically_invalid_generation_refuses(tmp_path, change, match):
    source = tmp_path / "source"
    generation(source, **change)
    with pytest.raises(IntegrityError, match=match):
        with stage_generation(source, family="federal-register", table="federal_register"):
            pytest.fail("invalid generation admitted")


def test_member_digest_and_pin_mismatch_refuse(tmp_path):
    source = tmp_path / "source"
    pin, _, _ = generation(source)
    with pytest.raises(IntegrityError):
        with stage_generation(source, family="federal-register", table="federal_register", expected_pin=replace(pin, artifact_digest="sha256:" + "0" * 64)):
            pytest.fail("wrong pin admitted")
    path = source / "federal_register.parquet"
    payload = path.read_bytes()
    path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    with pytest.raises(IntegrityError, match="digest"):
        with stage_generation(source, family="federal-register", table="federal_register"):
            pytest.fail("changed member admitted")


def test_columns_carry_table_profile_types_while_the_descriptor_keeps_duckdb_names(tmp_path):
    source = tmp_path / "source"
    data = pa.table({"document_number": ["2026-1"], "publication_date": ["2026-09-25"],
                     "signed_at": pa.array([datetime(2026, 9, 25, tzinfo=timezone.utc)], pa.timestamp("us", "UTC"))})
    columns = [["document_number", "VARCHAR"], ["publication_date", "VARCHAR"], ["signed_at", "TIMESTAMP WITH TIME ZONE"]]
    generation(source, data=data, columns=columns)
    with stage_generation(source, family="federal-register", table="federal_register") as admitted:
        assert admitted.columns == (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"),
                                    ("signed_at", "TIMESTAMPTZ"))
        TableSchema("federal-register:1", admitted.columns)


def test_single_column_contract_and_unknown_composite_rule(tmp_path):
    source = tmp_path / "single"
    generation(source, family="bill-family", table="congress_bills")
    with stage_generation(source, family="bill-family", table="congress_bills") as admitted:
        assert admitted.key == KeySpelling("value", "1", ("bill_id",))
    source = tmp_path / "composite"
    generation(source, family="bill-family", table="bill_actions")
    with pytest.raises(IntegrityError, match="versioned spicy-docs key spelling"):
        with stage_generation(source, family="bill-family", table="bill_actions"):
            pytest.fail("undeclared composite spelling invented")


@pytest.mark.parametrize("attack", ["member-path", "prefix-path", "symlink", "malformed"])
def test_unsafe_or_malformed_input_never_escapes_staging(tmp_path, attack):
    source = tmp_path / "source"
    pin, member, description = generation(source)
    if attack == "member-path":
        manifest = json.loads((source / "members.json").read_bytes())
        manifest["members"][0]["objectKey"] = "../outside.parquet"
        payload = canonical_json_bytes(manifest)
        (source / "members.json").write_bytes(payload)
        root = json.loads((source / "artifact.json").read_bytes())
        root["memberManifests"][0].update(byteSize=len(payload), sha256="sha256:" + hashlib.sha256(payload).hexdigest())
        (source / "artifact.json").write_bytes(canonical_json_bytes(stamp_root(root)))
    elif attack == "prefix-path":
        base = tmp_path / "published"
        pointer = publication(base, source, pin, member, description)
        pointer["families"]["federal-register"]["prefix"] = "../source"
        (base / "publication.json").write_bytes(canonical_json_bytes(pointer))
        source = base
    elif attack == "symlink":
        original = source / "federal_register.parquet"
        original.rename(tmp_path / "outside.parquet")
        original.symlink_to(tmp_path / "outside.parquet")
    else:
        (source / "artifact.json").write_bytes(b'{"not-json":')
    with pytest.raises(IntegrityError):
        with stage_generation(source, family="federal-register", table="federal_register"):
            pytest.fail("unsafe source admitted")
    assert not (tmp_path / "outside.parquet").exists() or attack == "symlink"


def test_aggregate_bound_removes_partial_staging(tmp_path):
    source, scratch = tmp_path / "source", tmp_path / "scratch"
    generation(source)
    scratch.mkdir()
    with pytest.raises(LimitExceededError):
        with stage_generation(source, family="federal-register", table="federal_register", directory=scratch, max_bytes=100):
            pytest.fail("unbounded stage admitted")
    assert not list(scratch.iterdir())


def test_https_uses_existing_transport_and_checks_same_pointer(tmp_path, monkeypatch):
    source, base = tmp_path / "source", tmp_path / "published"
    pin, member, description = generation(source)
    publication(base, source, pin, member, description)
    requests = []
    def respond(request):
        requests.append(str(request.url))
        path = base / request.url.path.removeprefix("/data/")
        return httpx.Response(200, stream=httpx.ByteStream(path.read_bytes()), request=request)
    clients = []
    def fetcher(config):
        client = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return HttpsContentFetcher(client, config)
    monkeypatch.setattr(HttpsContentFetcher, "from_httpx", fetcher)
    with stage_generation("https://example.test/data", family="federal-register", table="federal_register") as admitted:
        assert admitted.pin == pin and pq.read_table(admitted.path).num_rows == 1
    assert all(client.is_closed for client in clients)
    assert len(requests) == 4 and all(url.startswith("https://example.test/data/") for url in requests)
