"""Table-shaped states admit a producer generation by reference (decision 0007, C27).

A generation's member is registered unrewritten; its rows get member keys and
occurrences under docspec-table-row/1, spelled natively and equal to the Python
reference. One metadata unit publishes each generation. A later generation of
a dataset carries every unchanged occurrence forward and mints only added and
changed rows; an occurrence that reappears is adopted, never generated again
(ruling R1(b)). Every reader serves the rows through the membership, and C28's
by-identity reads resolve occurrences through the minted-occurrence index.
"""

from datetime import date
import sqlite3
import shutil
import struct

import httpx
import pyarrow as pa
import pytest
from rulespec_artifacts import canonical_json_bytes

from docspec.adapters.content_fetchers.https import HttpsContentFetcher
from docspec.adapters.storage.table_occurrences import lookup_occurrences, reference_identity
from docspec.domain import core
from docspec.domain.core_admission import inline_occurrence_payload
from docspec.domain.references import LayerRef
from docspec.domain.table_rows import KeySpelling, TableIdentity, table_row_bytes, table_row_value
from docspec.errors import IntegrityError, StaleBaseError
from docspec.runtime import CoreWorkspace
from tests.support.generations import generation, publication

FAMILY, TABLE = "federal-register", "federal_register"
NAN = struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0]
SCHEMA = pa.schema([("document_number", pa.string()), ("publication_date", pa.string()), ("title", pa.string()),
                    ("pages", pa.int32()), ("ratio", pa.float64()), ("signed", pa.date32()), ("flag", pa.bool_()),
                    ("topics", pa.list_(pa.string()))])
ROWS = [
    {"document_number": "2026-00001", "publication_date": "2026-09-01", "title": "A rule", "pages": 3,
     "ratio": NAN, "signed": date(2026, 8, 30), "flag": True, "topics": ["air", None]},
    {"document_number": "2026-\x1f02", "publication_date": "2026-09-02", "title": "Controls \x00\x1f\"\\u001F",
     "pages": -(2**31), "ratio": -0.0, "signed": date(1, 1, 1), "flag": False, "topics": []},
    {"document_number": "2026-00003", "publication_date": "2026-09-03", "title": None, "pages": None,
     "ratio": 0.1, "signed": None, "flag": None, "topics": None},
]
COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("title", "VARCHAR"), ("pages", "INTEGER"),
           ("ratio", "DOUBLE"), ("signed", "DATE"), ("flag", "BOOLEAN"), ("topics", "VARCHAR[]"))
IDENTITY = TableIdentity(FAMILY, TABLE, KeySpelling("federal-register-source-record-id", "1",
                                                    ("document_number", "publication_date")), COLUMNS)


def write(path, values, schema=SCHEMA, **options):
    """Seal one generation of ``values`` at ``path``; return its pin."""
    return generation(path, pa.Table.from_pylist(values, schema=schema), **options)[0]


def expected(rows, identity=IDENTITY):
    """The Python reference: member key -> (occurrence URN, exact occurrence record bytes)."""
    result = {}
    for row in rows:
        key, _, urn = reference_identity(identity, row)
        result[key] = urn, inline_occurrence_payload(urn, table_row_bytes(row, identity.columns))
    return result


def ledger_counts(workspace):
    with sqlite3.connect(workspace.path / "ledger.sqlite") as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("records", "retention", "units", "links")}


def test_first_admission_publishes_one_unit_and_every_reader_serves_the_reference(tmp_path):
    source = tmp_path / "g1"
    pin = write(source, ROWS)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        before = ledger_counts(workspace)
        admitted = workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        after = ledger_counts(workspace)
        report = admitted.report
        assert report["pin"] == {"logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest}
        assert report["counts"] == {"rows": 3, "generated": 3, "adopted": 0, "added": 3, "removed": 0, "changed": 0,
                                    "carried": 0, "reminted": False}
        assert workspace.ledger.current("fr") == ("state", admitted.state_id)
        # One admission unit (plus its identity mark and the dataset pointer): no per-row records.
        assert after["records"] - before["records"] == 9
        request = workspace.generating_request(admitted.state_id)
        assert [item.label for item in request.inputs] == ["root", "members"]
        reference = expected(ROWS)
        assert {key: (entity.entity_id, entity) for key, entity in workspace.rows(admitted.state_id)} == {
            key: (urn, core.Entity(format_version=1, entity_id=urn, entity_type="occurrence",
                                   value=core.InlineValue(value=table_row_value(row, COLUMNS))))
            for row in ROWS for key, (urn, _) in [(reference_identity(IDENTITY, row)[0], reference[reference_identity(IDENTITY, row)[0]])]}
        with workspace.open_state(admitted.state_id) as reader:
            assert reader.record_count == 3
            with reader.relation() as relation:
                assert {key: (urn, bytes(record)) for key, urn, record in relation.fetchall()} == reference
            key = "2026-\x1f02@2026-09-02"
            assert reader.read_value(key, occurrence_id=reference[key][0]) == table_row_value(ROWS[1], COLUMNS)
            assert reader.lookup("absent@2026-01-01") is None
            with pytest.raises(LookupError):
                reader.read_value("absent@2026-01-01")
            assert [(key, urn) for key, urn, _ in reader.values()] == sorted((key, urn) for key, (urn, _) in reference.items())
            with reader.value_relation() as values:
                assert len(values.fetchall()) == 3
            with reader.table() as typed:
                assert typed.columns == ["member_key", "occurrence_id", *(name for name, _ in COLUMNS)]
                rows = {row[0]: row for row in typed.fetchall()}
            assert rows[key][1] == reference[key][0] and rows[key][2:5] == ("2026-\x1f02", "2026-09-02", ROWS[1]["title"])


def admit_all(workspace, tmp_path, generations, *, dataset="fr"):
    """Admit each generation's rows in order as one dataset; return the admissions."""
    admissions = []
    for index, rows in enumerate(generations):
        source = tmp_path / f"generation-{index}"
        write(source, rows)
        admissions.append(workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset=dataset))
    return admissions


def occurrences(workspace, state_id):
    return {key: entity.entity_id for key, entity in workspace.rows(state_id)}


def layer(workspace, state_id, name):
    """One named layer reference of a state's retained manifest."""
    with workspace.publisher.session() as session:
        return LayerRef.from_dict(workspace.states.manifest(session, state_id)[name])


def test_exact_retry_returns_the_state_without_writing(tmp_path):
    source = tmp_path / "g1"
    write(source, ROWS)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        files = sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file())
        counts = ledger_counts(workspace)
        again = workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        assert again == first
        assert sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file()) == files
        assert ledger_counts(workspace) == counts
        assert not list(workspace.path.glob("generation-*"))


def test_a_later_generation_carries_unchanged_occurrences_and_mints_only_its_delta(tmp_path):
    changed = {**ROWS[0], "title": "An amended rule"}
    added = {**ROWS[2], "document_number": "2026-00004"}
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first, second = admit_all(workspace, tmp_path, [ROWS, [changed, ROWS[1], added]])
        assert second.report["base"] == first.state_id
        assert second.report["counts"] == {"rows": 3, "generated": 2, "adopted": 1, "added": 1, "removed": 1,
                                           "changed": 1, "carried": 1, "reminted": False}
        before, after = occurrences(workspace, first.state_id), occurrences(workspace, second.state_id)
        assert after == {key: urn for key, (urn, _) in expected([changed, ROWS[1], added]).items()}
        assert after["2026-\x1f02@2026-09-02"] == before["2026-\x1f02@2026-09-02"]
        # The membership applies the delta to the base's files.
        old, new = (layer(workspace, state, "membership") for state in (first.state_id, second.state_id))
        assert set(workspace.records.data_files(old)) < set(workspace.records.data_files(new))
        with workspace.open_state(first.state_id) as older, workspace.open_state(second.state_id) as newer:
            assert [(key, urn) for key, urn, _ in newer.changes(older)] == [
                ("2026-00001@2026-09-01", after["2026-00001@2026-09-01"]),
                ("2026-00003@2026-09-03", None), ("2026-00004@2026-09-03", after["2026-00004@2026-09-03"])]
        comparison = workspace.compare(first.state_id, second.state_id)
        assert comparison["counts"] == {"added": 1, "removed": 1, "changed": 1}
        assert [row["value_changed"] for row in comparison["sample"]] == [True, True, True]


def test_a_restored_row_resolves_to_its_first_occurrence(tmp_path):
    changed = {**ROWS[0], "ratio": 0.0}
    restored = [ROWS[0], ROWS[1], {**ROWS[2], "flag": True}]
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first, second, third = admit_all(workspace, tmp_path, [ROWS, [changed, *ROWS[1:]], restored])
        key = "2026-00001@2026-09-01"
        states = [occurrences(workspace, admission.state_id) for admission in (first, second, third)]
        assert states[2][key] == states[0][key] != states[1][key]
        # A->B->A adopts the first occurrence: only the newly changed row is generated.
        assert third.report["counts"]["generated"] == 1 and third.report["counts"]["changed"] == 2
        found = lookup_occurrences(workspace.records, layer(workspace, third.state_id, "occurrences"), IDENTITY,
                                   [states[2][key]])
        assert found[states[2][key]].first_state_id == first.state_id


def test_a_schema_change_remints_every_row_and_reports_it(tmp_path):
    widened = pa.schema([*SCHEMA, ("rin", pa.string())])
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit_all(workspace, tmp_path, [ROWS])[0]
        source = tmp_path / "widened"
        write(source, [{**row, "rin": None} for row in ROWS], widened)
        second = workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        assert second.report["counts"] == {"rows": 3, "generated": 3, "adopted": 0, "added": 0, "removed": 0,
                                           "changed": 3, "carried": 0, "reminted": True}
        assert set(occurrences(workspace, first.state_id).values()).isdisjoint(occurrences(workspace, second.state_id).values())


def test_a_dated_key_component_spells_its_varchar_key(tmp_path):
    dated = pa.schema([("document_number", pa.string()), ("publication_date", pa.date32()), ("title", pa.string())])
    rows = [{"document_number": "2026-1", "publication_date": date(2026, 9, 25), "title": "x"},
            {"document_number": "0001-1", "publication_date": date(1, 1, 1), "title": None}]
    identity = TableIdentity(FAMILY, TABLE, IDENTITY.key, (("document_number", "VARCHAR"), ("publication_date", "DATE"),
                                                           ("title", "VARCHAR")))
    source = tmp_path / "dated"
    write(source, rows, dated)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admitted = workspace.admit_generation(source, family=FAMILY, table=TABLE)
        assert occurrences(workspace, admitted.state_id) == {key: urn for key, (urn, _) in expected(rows, identity).items()}
        assert set(occurrences(workspace, admitted.state_id)) == {"2026-1@2026-09-25", "0001-1@0001-01-01"}
        with workspace.open_state(admitted.state_id) as reader:
            assert reader.read_value("0001-1@0001-01-01")["publication_date"] == "0001-01-01"


def refused(path, name):
    """A generation the admission must refuse, and the error it names."""
    rows, schema, options, match = ROWS, SCHEMA, {}, None
    if name == "local-partial":
        options, match = {"status": "local-partial"}, "complete-family"
    elif name == "footer":
        options, match = {"rows": 4}, "footer row count"
    elif name == "descriptor":
        options, match = {"columns": [[column, "VARCHAR"] for column in SCHEMA.names]}, "footer schema"
    elif name == "duplicate":
        rows, match = [*ROWS, {**ROWS[1], "title": "again"}], "duplicate member key"
    elif name == "null-key":
        rows, match = [*ROWS, {**ROWS[1], "publication_date": None}], "NULL or empty component"
    elif name == "empty-key":
        rows, match = [*ROWS, {**ROWS[1], "document_number": ""}], "NULL or empty component"
    elif name == "unsupported":
        schema, match = pa.schema([*SCHEMA, ("small", pa.int16())]), "unsupported"
        rows = [{**row, "small": 1} for row in ROWS]
    write(path, rows, schema, **options)
    return match


@pytest.mark.parametrize("name", ["local-partial", "footer", "descriptor", "duplicate", "null-key", "empty-key", "unsupported"])
def test_an_inadmissible_generation_refuses_and_publishes_nothing(tmp_path, name):
    match = refused(tmp_path / "bad", name)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        counts = ledger_counts(workspace)
        with pytest.raises(IntegrityError, match=match):
            workspace.admit_generation(tmp_path / "bad", family=FAMILY, table=TABLE, dataset="fr")
        assert ledger_counts(workspace) == counts and workspace.ledger.current("fr") is None
        assert not list(workspace.path.glob("generation-*"))
        # Every refusal precedes registration: no table moved into the store.
        assert not list((workspace.path / "records").glob("iceberg/*"))


def test_a_wrong_pin_and_a_spelling_change_refuse(tmp_path):
    source, published = tmp_path / "g1", tmp_path / "published"
    pin, member, description = generation(source, pa.Table.from_pylist(ROWS, schema=SCHEMA))
    pointer = publication(published, source, pin, member, description)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        pointer["families"][FAMILY]["artifactDigest"] = "sha256:" + "0" * 64
        (published / "publication.json").write_bytes(canonical_json_bytes(pointer))
        with pytest.raises(IntegrityError, match="generation admission refused"):
            workspace.admit_generation(published, family=FAMILY, table=TABLE)
        workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        # The artifact declares value/1 over document_number: an implicit re-key.
        rekeyed = tmp_path / "rekeyed"
        write(rekeyed, ROWS, identity=["document_number"])
        with pytest.raises(IntegrityError, match="member-key spelling"):
            workspace.admit_generation(rekeyed, family=FAMILY, table=TABLE, dataset="fr")


def test_an_https_publication_admits_through_the_existing_transport(tmp_path, monkeypatch):
    source, published = tmp_path / "g1", tmp_path / "published"
    pin, member, description = generation(source, pa.Table.from_pylist(ROWS, schema=SCHEMA))
    publication(published, source, pin, member, description)

    def respond(request):
        return httpx.Response(200, stream=httpx.ByteStream((published / request.url.path.removeprefix("/data/")).read_bytes()),
                              request=request)
    monkeypatch.setattr(HttpsContentFetcher, "from_httpx", lambda config: HttpsContentFetcher(
        httpx.Client(transport=httpx.MockTransport(respond)), config))
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        remote = workspace.admit_generation("https://example.test/data", family=FAMILY, table=TABLE)
        local = workspace.admit_generation(source, family=FAMILY, table=TABLE)
        assert remote == local and remote.report["pin"]["artifactDigest"] == pin.artifact_digest


def authorize(workspace, keys, identity="remove-superseded"):
    """Publish a removal policy naming ``keys``."""
    policy = core.RetentionPolicy(format_version=1, policy_id=identity,
                                  description={"remove": [list(key) for key in keys], "collect_unreferenced": False})
    workspace.retain([policy], unit_id=identity, roots=[("retention_policy", identity)])
    return identity


def admission_keys(state_id):
    """The retained keys that bind an admitted state: it, its representation and the result generating it."""
    return [("result", state_id + ":execution:result"), ("state", state_id), ("state_representation", state_id + ":physical")]


def test_members_resolve_through_the_index_and_a_superseded_generation_is_removable(tmp_path):
    changed = {**ROWS[0], "title": "An amended rule"}
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first, second = admit_all(workspace, tmp_path, [ROWS, [changed, *ROWS[1:]]])
        older, newer = occurrences(workspace, first.state_id), occurrences(workspace, second.state_id)
        carried, superseded = newer["2026-\x1f02@2026-09-02"], older["2026-00001@2026-09-01"]
        reference = dict([*expected(ROWS).values(), *expected([changed]).values()])
        with workspace.publisher.session() as session:
            located = workspace.states.find_members(session, [carried, superseded, "urn:docspec:table-occurrence:v1:" + "0" * 64])
            assert set(located) == {carried, superseded}
            # Each occurrence resolves in the newest state holding it, with its exact bytes.
            assert located[carried][0].table == layer(workspace, second.state_id, "table")
            assert located[superseded][0].table == layer(workspace, first.state_id, "table")
            assert {urn: location[2] for urn, location in located.items()} == {urn: reference[urn] for urn in located}
            rows = next(session.read_records([("entity", carried), ("entity", superseded)]))
            assert [row.value for row in rows] == [core.Entity(format_version=1, entity_id=urn, entity_type="occurrence",
                value=core.InlineValue(value=table_row_value(row, COLUMNS))) for urn, row in ((carried, ROWS[1]), (superseded, ROWS[0]))]
        # A reference by identity pins the member once, keeping its exact bytes in the ledger.
        workspace.retain([], unit_id="pin", roots=[("entity", superseded)])
        with sqlite3.connect(workspace.path / "ledger.sqlite") as connection:
            assert connection.execute("SELECT payload, source_layer FROM records WHERE kind='entity' AND record_id=?",
                                      (superseded,)).fetchone() == (reference[superseded], None)
        # R3: the superseded generation is removable once the next is current.
        table = layer(workspace, first.state_id, "table")
        member = next(locator for locator in workspace.records.data_files(table))
        outcome = workspace.maintenance.remove_under_policy(
            "remove-first", authorize(workspace, admission_keys(first.state_id)), admission_keys(first.state_id))
        assert outcome["deleted"] > 0 and not (workspace.path / "records" / member).exists()
        with workspace.open_state(second.state_id) as reader:
            assert {key: urn for key, urn, _ in reader.values()} == newer
        with workspace.publisher.session() as session:
            rows = next(session.read_records([("entity", carried), ("entity", superseded)]))
            assert [row.value.entity_id for row in rows] == [carried, superseded]


def test_a_relocated_workspace_reads_and_a_flipped_member_byte_refuses(tmp_path):
    source = tmp_path / "g1"
    write(source, ROWS)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admitted = workspace.admit_generation(source, family=FAMILY, table=TABLE)
        member = workspace.records.data_files(layer(workspace, admitted.state_id, "table"))[0]
    shutil.copytree(tmp_path / "workspace", tmp_path / "moved")
    with CoreWorkspace(tmp_path / "moved", create=False) as moved, moved.open_state(admitted.state_id) as reader:
        assert {key: urn for key, urn, _ in reader.values()} == {key: urn for key, (urn, _) in expected(ROWS).items()}
    path = tmp_path / "moved" / "records" / member
    payload = bytearray(path.read_bytes())
    payload[len(payload) // 2] ^= 1
    path.write_bytes(payload)
    with CoreWorkspace(tmp_path / "moved", create=False) as moved:
        with pytest.raises(IntegrityError, match="differs from its checksum"):
            with moved.open_state(admitted.state_id):
                pytest.fail("a flipped member byte was read")


def test_put_and_remove_on_a_table_shaped_base_refuse(tmp_path):
    source = tmp_path / "g1"
    write(source, ROWS)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admitted = workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        with pytest.raises(IntegrityError, match="admitting its next generation"):
            workspace.revise(core.Revision(format_version=1, revision_id="remove", base_state_id=admitted.state_id,
                                           result_state_id="revised", edits=(core.Remove(sequence=0, member_key="2026-00003@2026-09-03"),)))
        with pytest.raises(IntegrityError, match="admitting its next generation"):
            workspace.upsert(admitted.state_id, [("2026-00009@2026-09-09", {"title": "put"})], batch_id="put", dataset="fr")
        assert workspace.ledger.current("fr") == ("state", admitted.state_id)


def test_the_dataset_pointer_advances_only_over_its_admission_base(tmp_path, monkeypatch):
    changed = {**ROWS[0], "title": "An amended rule"}
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = admit_all(workspace, tmp_path, [ROWS])[0]
        other = tmp_path / "other"
        write(other, ROWS[1:])
        elsewhere = workspace.admit_generation(other, family=FAMILY, table=TABLE)
        admit = workspace.states.admit_table

        def overtaken(*args, **kwargs):
            # Another writer moves the dataset while this admission computes.
            result = admit(*args, **kwargs)
            workspace.maintenance.select_current("overtake", "fr", ("state", elsewhere.state_id), ("state", first.state_id))
            return result
        monkeypatch.setattr(workspace.states, "admit_table", overtaken)
        source = tmp_path / "g2"
        write(source, [changed, *ROWS[1:]])
        with pytest.raises(StaleBaseError):
            workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        # Its state is published over its own base, which is no longer current.
        with pytest.raises(StaleBaseError):
            workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset="fr")
        assert workspace.ledger.current("fr") == ("state", elsewhere.state_id)
