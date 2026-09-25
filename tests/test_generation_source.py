"""Generation staging binds producer evidence without rewriting or trusting its pointer."""

from dataclasses import replace
import hashlib
import json
import shutil

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rulespec_artifacts import (ArtifactPin, LocalMemberSource, Producer, build_artifact_root,
    canonical_json_bytes, describe_member, stamp_root, write_member_manifest)

from docspec.adapters.content_fetchers.https import HttpsContentFetcher
from docspec.adapters.generation_source import stage_generation
from docspec.errors import IntegrityError, LimitExceededError

# Rulespec requires a producer pinned by a published digest or full Git object ID.
_IMPLEMENTATION = "git+https://example.test/spicy-regs@" + "1" * 40
PRODUCER = Producer("spicy-regs", _IMPLEMENTATION, "urn:test:verifier", "1", _IMPLEMENTATION)


def generation(path, *, table="federal_register", family="federal-register", status="complete-family",
               kind="spicy-regs-rollup-generation", columns=None, rows=None, identity=None):
    path.mkdir(parents=True)
    values = {"document_number": ["2026-1"], "publication_date": ["2026-09-25"], "title": ["A rule"]}
    if table == "congress_bills":
        values = {"bill_id": ["119-hr-1"], "title": ["A bill"]}
    elif table == "bill_actions":
        values = {"bill_id": ["119-hr-1"], "action_index": ["1"]}
    data = pa.table(values)
    pq.write_table(data, path / (table + ".parquet"))
    member = describe_member(LocalMemberSource(path), object_key=table + ".parquet", role="table",
                             media_type="application/vnd.apache.parquet", record_count=data.num_rows if rows is None else rows)
    with (path / "members.json").open("wb") as output:
        manifest = write_member_manifest(output, scope_kind="global", scope_id=family, object_key="members.json", members=[member])
    description = {"columns": columns or [[name, "VARCHAR"] for name in values], "rows": member.record_count}
    if identity is not None:
        description["identity"] = identity
    root = build_artifact_root(kind=kind, spec={"family": family, "publicationStatus": status, "tables": {member.object_key: description}},
                              producer=PRODUCER, manifests=[manifest])
    (path / "artifact.json").write_bytes(canonical_json_bytes(root))
    return ArtifactPin(root["logicalId"], root["artifactDigest"]), member, description


def publication(base, source, pin, member, description, *, prefix="generations/federal-register/current"):
    target = base / prefix
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    value = {"format": "spicy-regs-publication", "version": 1, "families": {"federal-register": {
        "prefix": prefix, "logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest,
        "tables": {member.object_key: {**description, "sha256": member.sha256, "byteSize": member.byte_size}}}}}
    (base / "publication.json").write_bytes(canonical_json_bytes(value))
    return value


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
        assert admitted.key_fields == ("document_number", "publication_date")
        assert (admitted.key_spelling_id, admitted.key_spelling_version) == ("federal-register-source-record-id", "1")
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


def test_single_column_contract_and_unknown_composite_rule(tmp_path):
    source = tmp_path / "single"
    generation(source, family="bill-family", table="congress_bills")
    with stage_generation(source, family="bill-family", table="congress_bills") as admitted:
        assert admitted.key_fields == ("bill_id",)
        assert (admitted.key_spelling_id, admitted.key_spelling_version) == ("value", "1")
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
