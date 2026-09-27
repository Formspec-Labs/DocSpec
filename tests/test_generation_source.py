"""Generation staging binds producer evidence without rewriting or trusting its pointer."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rulespec_artifacts import LocalMemberSource, canonical_json_bytes, describe_member, stamp_root

from spicy_docs.schemas import TABLE_CONTRACTS
from spicy_docs.schemas.tables import KEY_SPELLINGS, table_contract

from docspec.adapters import generation_source
from docspec.adapters.generation_source import stage_generation
from docspec.adapters.storage import table_sql
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import KeySpelling, TableIdentity
from docspec.errors import IntegrityError, LimitExceededError
from tests.support.generations import (family_generation, generation, publication, publish, seal_generation,
    serve_https, split_members)


def test_local_generation_preserves_source_and_returns_exact_evidence(tmp_path):
    source, scratch = tmp_path / "source", tmp_path / "staging"
    scratch.mkdir()
    pin, member, _ = generation(source)
    originals = {path.name: path.read_bytes() for path in source.iterdir()}
    with stage_generation(source, family="federal-register", table="federal_register", directory=scratch, expected_pin=pin) as admitted:
        [(staged, descriptor)] = admitted.members
        assert admitted.pin == pin and descriptor == member and admitted.partition_columns == ()
        assert staged.read_bytes() == originals["federal_register.parquet"]
        assert admitted.root_bytes == originals["artifact.json"]
        assert admitted.manifest_bytes == originals["members.json"]
        assert admitted.record_count == 1
        assert admitted.columns == (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("title", "VARCHAR"))
        assert admitted.key == KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
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
    requests, clients = serve_https(monkeypatch, base)
    with stage_generation("https://example.test/data", family="federal-register", table="federal_register") as admitted:
        assert admitted.pin == pin and pq.ParquetFile(admitted.members[0].path).metadata.num_rows == 1
    assert all(client.is_closed for client in clients)
    # No version 2 is served, so its 404 falls back to version 1.
    assert [url.rsplit("/", 1)[1] for url in requests[:2]] == ["publication.v2.json", "publication.json"]
    assert len(requests) == 5 and all(url.startswith("https://example.test/data/") for url in requests)


FEDERAL_REGISTER_COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("title", "VARCHAR"))


def contracts(monkeypatch, entries, spellings=None):
    """Stand in for an installed spicy-docs whose contracts and key-spelling registry add ``entries`` and ``spellings``."""
    monkeypatch.setattr(generation_source, "TABLE_CONTRACTS", {**TABLE_CONTRACTS, **entries})
    registry = {**KEY_SPELLINGS, **(spellings or {})}
    monkeypatch.setattr(generation_source, "KEY_SPELLINGS", registry)
    monkeypatch.setattr(table_sql, "KEY_SPELLINGS", registry)


def test_a_typed_contract_refuses_a_footer_of_other_types(tmp_path, monkeypatch):
    columns = {"row_id": "The key.", "open": "A flag.", "pages": "A count.", "posted": "An instant.",
               "topics": "Labels.", "title": "Text."}
    contracts(monkeypatch, {"typed_rows": table_contract(
        "typed_rows", grain="One row.", identity=("row_id",), version_column=None, columns=columns,
        key_spelling="value/1", types={"open": "BOOLEAN", "pages": "INTEGER", "posted": "TIMESTAMPTZ",
                                       "topics": "VARCHAR[]"})})
    typed = {"row_id": ["r-1"], "open": [True], "pages": pa.array([3], pa.int32()),
             "posted": pa.array([datetime(2026, 9, 25, tzinfo=timezone.utc)], pa.timestamp("us", "UTC")),
             "topics": [["a", "b"]], "title": ["A title"]}
    generation(tmp_path / "typed", pa.table(typed), family="typed-family", table="typed_rows")
    with stage_generation(tmp_path / "typed", family="typed-family", table="typed_rows") as admitted:
        assert dict(admitted.columns) == {"row_id": "VARCHAR", "open": "BOOLEAN", "pages": "INTEGER",
                                          "posted": "TIMESTAMPTZ", "topics": "VARCHAR[]", "title": "VARCHAR"}
    # A v1-style member spells the flag and the count as text; the typed contract refuses it.
    generation(tmp_path / "text", pa.table({**typed, "open": ["true"], "pages": pa.array([3], pa.int64())}),
               family="typed-family", table="typed_rows")
    with pytest.raises(IntegrityError, match="open is VARCHAR, not BOOLEAN; pages is BIGINT, not INTEGER"):
        with stage_generation(tmp_path / "text", family="typed-family", table="typed_rows"):
            pytest.fail("mistyped member admitted")


def test_the_federal_register_contract_and_the_decision_0003_fallback_agree(monkeypatch):
    row = {"document_number": "00-111", "publication_date": "2000-01-18", "title": "A notice"}
    fallback = generation_source._key_rule("federal-register", "federal_register", {}, FEDERAL_REGISTER_COLUMNS)
    fallback_key = table_sql.reference_member_key(
        TableIdentity("federal-register", "federal_register", fallback, FEDERAL_REGISTER_COLUMNS), row)
    spelled = []

    def federal_register_record_key(parts):  # the reference spicy-docs registers for the contract
        spelled.append(parts)
        return f"{parts[0]}@{parts[1]}"
    contracts(monkeypatch, {"federal_register": SimpleNamespace(
        identity=("document_number", "publication_date"), key_spelling="federal-register-source-record-id/1")},
        {"federal-register-source-record-id/1": federal_register_record_key})
    declared = generation_source._key_rule("federal-register", "federal_register", {}, FEDERAL_REGISTER_COLUMNS)
    declared_key = table_sql.reference_member_key(
        TableIdentity("federal-register", "federal_register", declared, FEDERAL_REGISTER_COLUMNS), row)
    assert declared == fallback == KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
    assert declared_key == fallback_key == "00-111@2000-01-18"
    assert spelled == [("00-111", "2000-01-18")]  # the contract path spelled through spicy-docs' own reference


def test_a_contract_must_declare_a_spelling_docspec_compiles(monkeypatch):
    single, pair = (("bill_id", "VARCHAR"), ("title", "VARCHAR")), (("a", "VARCHAR"), ("b", "VARCHAR"))
    contracts(monkeypatch, {
        "undeclared": SimpleNamespace(identity=("bill_id",), key_spelling=None),
        "composite": SimpleNamespace(identity=("a", "b"), key_spelling=None),
        "unregistered": SimpleNamespace(identity=("bill_id",), key_spelling="value/9"),
        "uncompiled": SimpleNamespace(identity=("bill_id",), key_spelling="padded/1")}, {"padded/1": lambda parts: parts[0]})
    for table, columns, match in [("undeclared", single, "declares no key spelling for its identity"),
                                  ("composite", pair, "versioned spicy-docs key spelling"),
                                  ("unregistered", single, "declares no key spelling value/9"),
                                  ("uncompiled", single, "not declared for these fields")]:
        with pytest.raises(IntegrityError, match=match):
            generation_source._key_rule("family", table, {}, columns)
    # The artifact's own identity comes first, and one field spells as its value.
    assert generation_source._key_rule("family", "undeclared", {"identity": ["title"]}, single) == KeySpelling("value", "1", ("title",))


# The bill family once bill_sections splits (spicy-regs multi-file design): congress_bills stays one file and
# bill_sections is one member per Congress. The installed contract declares at-joined/1, which DocSpec does not
# compile yet, so these rows declare a one-field identity of their own.
SECTIONS = pa.table({"section_id": ["118-hr-1#1", "118-hr-2#1", "119-s-5#1", "119-s-5#2"],
                     "congress": ["118", "118", "119", "119"], "body": ["a", "b", "c", "d"]})
BILLS = pa.table({"bill_id": ["118-hr-1", "119-s-5"], "title": ["A bill", "Another bill"]})
SPLIT = {"bill_sections": ["congress"]}
IDENTITY = {"bill_sections": {"identity": ["section_id"]}}


def bill_family(path, sections=None, *, overrides=None, **options):
    """Seal the bill family with ``sections`` (default: SECTIONS split by congress); return its pin, members, descriptors."""
    return family_generation(path, {"congress_bills": BILLS, "bill_sections": sections or split_members(SECTIONS, "congress")},
                             partitions=SPLIT, overrides={**IDENTITY, **(overrides or {})}, **options)


def published_bill_family(tmp_path, sections=None, *, versions=(2, 1), **options):
    """Publish a sealed bill family under ``tmp_path/published``; return the base and its version-2 pointer."""
    pin, members, descriptions = bill_family(tmp_path / "source", sections, **options)
    base = tmp_path / "published"
    return base, publish(base, tmp_path / "source", pin, members, descriptions, versions=versions)[0]


def test_version_2_stages_a_single_file_table_exactly_as_version_1(tmp_path):
    """Everything after the pointer is the same evidence: only the version-2 key and version differ."""
    pin, member, description = generation(tmp_path / "source")
    staged = {}
    for versions in ((1,), (2, 1)):
        base = tmp_path / f"published-{len(versions)}"
        publish(base, tmp_path / "source", pin, [member], {member.object_key: description}, family="federal-register",
                versions=versions)
        with stage_generation(base, family="federal-register", table="federal_register") as admitted:
            staged[versions] = replace(admitted, members=tuple((path.read_bytes(), descriptor)
                                                                for path, descriptor in admitted.members))
    v1, v2 = (tmp_path / "published-2" / key for key in ("publication.json", "publication.v2.json"))
    assert json.loads(v2.read_bytes()) == {**json.loads(v1.read_bytes()), "version": 2}
    assert staged[(1,)] == staged[(2, 1)]


def test_a_mixed_family_stages_either_table_through_version_2_and_its_derived_version_1_refuses(tmp_path):
    base, _ = published_bill_family(tmp_path)
    with stage_generation(base, family="bill-family", table="congress_bills") as bills:
        assert [descriptor.object_key for _, descriptor in bills.members] == ["congress_bills.parquet"]
        assert bills.partition_columns == () and bills.record_count == 2
    with stage_generation(base, family="bill-family", table="bill_sections") as sections:
        assert [descriptor.object_key for _, descriptor in sections.members] == [
            "bill_sections/congress=118/part-000000.parquet", "bill_sections/congress=119/part-000000.parquet"]
        assert sections.partition_columns == ("congress",) and sections.record_count == 4
        assert [pq.ParquetFile(path).metadata.num_rows for path, _ in sections.members] == [2, 2]
        assert sections.key == KeySpelling("value", "1", ("section_id",))
    # Version 1 omits the split table, so a reader of version 1 alone refuses the whole family, congress_bills too.
    (base / "publication.v2.json").unlink()
    with pytest.raises(IntegrityError, match="table set differs"):
        with stage_generation(base, family="bill-family", table="congress_bills"):
            pytest.fail("a family missing its split table admitted")


@pytest.mark.parametrize("key,value,match", [
    ("publication.v2.json", 3, "unsupported generation publication pointer"),
    ("publication.v2.json", 1, "unsupported generation publication pointer"),
    ("publication.json", 2, "unsupported generation publication pointer"),
    ("publication.json", "members", "version-1 publication pointer lists a split table"),
])
def test_a_pointer_of_another_version_refuses(tmp_path, key, value, match):
    base, pointer = published_bill_family(tmp_path)
    if key == "publication.json":
        (base / "publication.v2.json").unlink()
    written = {**pointer, "version": 1} if value == "members" else {**pointer, "version": value}
    (base / key).write_bytes(canonical_json_bytes(written))
    with pytest.raises(IntegrityError, match=match):
        with stage_generation(base, family="bill-family", table="congress_bills"):
            pytest.fail("pointer of another version admitted")


def test_https_reads_version_2_and_falls_back_only_when_it_is_absent(tmp_path, monkeypatch):
    base, _ = published_bill_family(tmp_path)
    requests, _ = serve_https(monkeypatch, base)
    with stage_generation("https://example.test/data", family="bill-family", table="bill_sections") as admitted:
        assert len(admitted.members) == 2
    assert "https://example.test/data/publication.json" not in requests
    # A version-2 read that fails for any reason but absence refuses; it never falls back to version 1.
    requests, _ = serve_https(monkeypatch, base, status={"publication.v2.json": 503})
    with pytest.raises(IntegrityError, match="retryable"):
        with stage_generation("https://example.test/data", family="bill-family", table="congress_bills"):
            pytest.fail("admitted through version 1 while version 2 failed")
    assert [url.rsplit("/", 1)[1] for url in requests] == ["publication.v2.json"]


def test_members_listed_in_any_order_admit(tmp_path):
    base, pointer = published_bill_family(tmp_path)
    table = pointer["families"]["bill-family"]["tables"]["bill_sections.parquet"]
    table["members"].reverse()
    (base / "publication.v2.json").write_bytes(canonical_json_bytes(pointer))
    with stage_generation(base, family="bill-family", table="bill_sections") as admitted:
        assert [descriptor.object_key for _, descriptor in admitted.members] == sorted(
            member["key"] for member in table["members"])


def _lying_index(change):
    """Edit the version-2 entry of bill_sections to disagree with its artifact."""
    def edit(table):
        members = table["members"]
        if change == "outside":
            members[0]["key"] = "../bill_sections/congress=118/part-000000.parquet"
        elif change == "duplicate":
            members.append(dict(members[0]))
            table.update(rows=table["rows"] + members[0]["rows"], byteSize=table["byteSize"] + members[0]["byteSize"])
        elif change == "missing":
            table.update(rows=table["rows"] - members[-1]["rows"], byteSize=table["byteSize"] - members[-1]["byteSize"])
            members.pop()
        elif change == "partition":
            members[0]["partition"] = {"congress": "117"}
        else:
            table["byteSize"] += 1
    return edit


@pytest.mark.parametrize("change", ["outside", "duplicate", "missing", "partition", "bytes"])
def test_a_version_2_entry_that_differs_from_the_artifact_refuses(tmp_path, change):
    base, pointer = published_bill_family(tmp_path)
    _lying_index(change)(pointer["families"]["bill-family"]["tables"]["bill_sections.parquet"])
    (base / "publication.v2.json").write_bytes(canonical_json_bytes(pointer))
    with pytest.raises(IntegrityError, match="publication table descriptor differs"):
        with stage_generation(base, family="bill-family", table="congress_bills"):
            pytest.fail("lying version-2 entry admitted")


@pytest.mark.parametrize("case,match", [
    ("partition-value", "differ from the partition congress=118"),
    ("key-grammar", "does not spell its partition"),
    ("key-column", "does not spell its partition"),
    ("undeclared-partition", "partition columns must be distinct declared columns"),
    ("rows-sum", "descriptor differs from its member"),
    ("member-count", "footer row count"),
    ("mixed-schemas", "footer schema differs"),
    ("duplicate-bytes", "same member bytes twice"),
    ("stray-member", "table set differs"),
    ("unsplit-nested", "table set differs"),
])
def test_a_split_table_that_disagrees_with_its_members_refuses(tmp_path, case, match):
    sections, options = split_members(SECTIONS, "congress"), {}
    if case == "partition-value":  # the 119th's rows filed under congress=118
        sections = [("congress=118/part-000000.parquet", sections[1][1]), ("congress=119/part-000000.parquet", sections[0][1])]
    elif case == "key-grammar":
        sections[0] = ("congress=118/sections.parquet", sections[0][1])
    elif case == "key-column":
        sections[0] = ("session=118/part-000000.parquet", sections[0][1])
    elif case == "undeclared-partition":
        options["overrides"] = {"bill_sections": {"partitionColumns": ["session"]}}
    elif case == "rows-sum":
        options["overrides"] = {"bill_sections": {"rows": 5}}
    elif case == "member-count":
        options["counts"] = {"bill_sections/congress=118/part-000000.parquet": 3}
        options["overrides"] = {"bill_sections": {"rows": 5}}
    elif case == "mixed-schemas":
        sections[0] = (sections[0][0], sections[0][1].append_column("extra", pa.array(["x", "y"])))
    elif case == "duplicate-bytes":  # the same file twice in one partition: its rows twice
        sections.append(("congress=119/part-000001.parquet", sections[1][1]))
    source = tmp_path / "source"
    _, members, descriptions = bill_family(source, sections, **options)
    if case == "stray-member":  # a Parquet file at the family root that no table declares
        pq.write_table(BILLS, source / "stray.parquet")
        stray = describe_member(LocalMemberSource(source), object_key="stray.parquet", role="table",
                                media_type="application/vnd.apache.parquet", record_count=2)
        seal_generation(source, "bill-family", [*members, stray], descriptions)
    elif case == "unsplit-nested":  # split members under a descriptor that declares no partitionColumns
        unsplit = {key: value for key, value in descriptions["bill_sections.parquet"].items() if key != "partitionColumns"}
        seal_generation(source, "bill-family", members, {**descriptions, "bill_sections.parquet": unsplit})
    with pytest.raises(IntegrityError, match=match):
        with stage_generation(source, family="bill-family", table="congress_bills"):
            pytest.fail("inconsistent split table admitted")


@pytest.mark.parametrize("statistics", [True, False])
def test_partition_values_are_checked_with_or_without_footer_statistics(tmp_path, statistics):
    bill_family(tmp_path / "good", write_statistics=statistics)
    with stage_generation(tmp_path / "good", family="bill-family", table="bill_sections") as admitted:
        assert admitted.record_count == 4
    wrong = [("congress=118/part-000000.parquet", SECTIONS.slice(0, 3))]  # holds a 119th-Congress row
    bill_family(tmp_path / "wrong", wrong, write_statistics=statistics)
    with pytest.raises(IntegrityError, match="differ from the partition congress=118"):
        with stage_generation(tmp_path / "wrong", family="bill-family", table="bill_sections"):
            pytest.fail("a member holding another partition's row admitted")
