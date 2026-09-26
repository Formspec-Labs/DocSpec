"""Typed derived layers (C29): caller rows become a table-shaped state in one unit, scoped by their definition.

Rows are Arrow batches in a declared schema carrying member_key and the source
occurrence each row derives from. They are written natively; each row's
occurrence follows docspec-table-row/1 under the definition's scope and equals
the Python reference. An incremental derive replaces the rows of the source
members it names, writes only rows whose occurrence changed and shares the
base's files. Retry and refusal follow C26; a one-to-many layer keys rows by
member-segment/1; a fusion layer finds the rows a changed input affects; an
older derived state is removable while a newer one shares its files.
"""

from datetime import date, datetime, timedelta, timezone
import sqlite3

import pyarrow as pa
import pytest

from docspec.adapters.storage import core_tables
from docspec.adapters.storage.batches import table_arrow_schema
from docspec.adapters.storage.core_states import CoreStateStorage, StateLayers
from docspec.adapters.storage.table_occurrences import reference_identity
from docspec.domain import core
from docspec.domain.core_admission import inline_occurrence_payload
from docspec.domain.identity import stable_urn
from docspec.domain.references import LayerRef
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import TableIdentity, table_row_bytes, table_row_value
from docspec.errors import IntegrityError, StaleBaseError
from docspec.runtime import CoreWorkspace
from tests.support.generations import generation

COLUMNS = (("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"), ("title", "VARCHAR"),
           ("identifiers", "VARCHAR[]"), ("published_on", "DATE"), ("signed_at", "TIMESTAMPTZ"), ("pages", "BIGINT"),
           ("withdrawn", "BOOLEAN"), ("score", "DOUBLE"))
SCHEMA = TableSchema("prepared-test:1", COLUMNS)
SOURCE = {"a": {"title": "A rule"}, "b\x1f\"quoted\"": {"title": "Controls"}, "c": {"title": None},
          "d": {"title": "Four"}, "e": {"title": "Five"}}


def definition(version="1"):
    """A caller's definition: its ID scopes every occurrence the derive mints."""
    configuration = {"lookup": "agencies:1", "version": version}
    return core.OperationDefinition(format_version=1, definition_id=stable_urn("typed-derive-test", configuration),
                                    implementation_id="test.prepare", implementation_version=version,
                                    operation_kind="transformation", configuration=configuration)


def prepared(key, occurrence, **changes):
    """One typed prepared row for source member ``key``; every type the layer holds, a list with a NULL element."""
    row = {"member_key": key, "source_occurrence_id": occurrence, "title": f"Title {key}",
           "identifiers": [key, "shared", None] if key == "a" else [], "published_on": date(2026, 9, len(key)),
           "signed_at": datetime(2026, 9, 25, 1, 2, 3, 400, tzinfo=timezone(timedelta(hours=-4))) if key == "a" else None,
           "pages": 2**53 + 1 if key == "a" else len(key), "withdrawn": key == "c", "score": -0.0 if key == "a" else 0.5}
    return {**row, **changes}


def batches(rows, columns=COLUMNS):
    """Arrow batches of two rows each, as a caller streams them."""
    return pa.Table.from_pylist(rows, schema=table_arrow_schema(columns)).to_batches(max_chunksize=2)


def source_state(workspace, name="source", values=SOURCE):
    """Retain a JSON source state; return its members' occurrences."""
    workspace.create(name, list(values.items()))
    return {key: entity.entity_id for key, entity in workspace.rows(name)}


SOURCE_UPDATE = core.OperationDefinition(format_version=1, definition_id="urn:test:source-update",
                                         implementation_id="test.source-update", implementation_version="1",
                                         operation_kind="transformation", configuration={})


def revise(workspace, base, changes, *, removals=()):
    """Revise a source state as a producer's update would; unchanged members keep their occurrences."""
    state = workspace.derive(list(changes.items()), batch_id=f"{base}:{sorted(changes)}", definition=SOURCE_UPDATE,
                             inputs=(), base_state_id=base, removals=removals)
    return state.state_id, {key: entity.entity_id for key, entity in workspace.rows(state.state_id)}


def lookup(workspace):
    """Retain one lookup value; return its whole-input binding."""
    workspace.create("lookups", [("agencies", {"EPA": "Environmental Protection Agency"})])
    return core.WholeInput(label="agencies", entity_id=dict(workspace.rows("lookups"))["agencies"].entity_id)


def derive(workspace, rows, batch_id, *, source="source", version="1", columns=COLUMNS, schema=SCHEMA, **options):
    inputs = options.pop("inputs", (core.StateInput(label="source", state_id=source),))
    return workspace.derive_table(batches(rows, columns), schema=schema, batch_id=batch_id,
                                  definition=definition(version), inputs=inputs, **options)


def expected(rows, version="1", schema=SCHEMA):
    """The Python reference: member key -> (occurrence URN, exact occurrence record bytes, JSON value)."""
    identity = TableIdentity.derived(definition(version).definition_id, schema)
    result = {}
    for row in rows:
        key, _, urn = reference_identity(identity, row)
        result[key] = urn, inline_occurrence_payload(urn, table_row_bytes(row, identity.columns)), \
            table_row_value(row, identity.columns)
    return result


def ledger_counts(workspace):
    with sqlite3.connect(workspace.path / "ledger.sqlite") as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("records", "retention", "units")}


def layer(workspace, state_id, name):
    with workspace.publisher.session() as session:
        return LayerRef.from_dict(workspace.states.manifest(session, state_id)[name])


def typed_rows(reader):
    """The typed reader's rows through Arrow, which keeps zoned timestamps without pytz."""
    with reader.table() as relation:
        return relation.order("member_key").to_arrow_table().to_pylist()


def test_a_typed_derive_publishes_one_unit_and_every_reader_serves_the_reference(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences, agencies = source_state(workspace), lookup(workspace)
        rows = [prepared(key, occurrence) for key, occurrence in occurrences.items()]
        derived = derive(workspace, rows, "first", inputs=(core.StateInput(label="source", state_id="source"), agencies))
        reference = expected(rows)
        assert derived.report["counts"] == {"rows": 5, "added": 5, "changed": 0, "removed": 0, "unchanged": 0,
                                            "generated": 5, "adopted": 0}
        request = workspace.generating_request(derived.state_id)
        assert [item.label for item in request.inputs] == ["source", "agencies", "rows"]
        assert request.definition_id == definition().definition_id
        assert dict(workspace.rows(derived.state_id)) == {
            key: core.Entity(format_version=1, entity_id=urn, entity_type="occurrence", value=core.InlineValue(value=value))
            for key, (urn, _, value) in reference.items()}
        with workspace.open_state(derived.state_id) as reader:
            # A whole state is spelled natively, named keys by the Python reference: the bytes are the same.
            for scope in (None, list(reference)):
                with reader.relation(member_keys=scope) as relation:
                    assert {key: (urn, bytes(record)) for key, urn, record in relation.fetchall()} == {
                        key: (urn, payload) for key, (urn, payload, _) in reference.items()}
            key = "b\x1f\"quoted\""
            assert reader.read_value(key, occurrence_id=reference[key][0]) == reference[key][2]
            assert reader.lookup("absent") is None
            assert [(key, urn) for key, urn, _ in reader.values()] == sorted((key, urn) for key, (urn, _, _) in reference.items())
            # The typed reader keeps every column's native type, member_key first.
            assert typed_rows(reader) == [{"member_key": row["member_key"], "occurrence_id": reference[row["member_key"]][0],
                                           **{name: row[name] for name, _ in COLUMNS[1:]}}
                                          for row in sorted(rows, key=lambda row: row["member_key"])]
        # An occurrence read by identity resolves through the definition's index.
        urn = reference["a"][0]
        with workspace.publisher.session() as session:
            assert next(session.read_records([("entity", urn)]))[0].value.value.value == reference["a"][2]
        # The same rows under another definition are other occurrences.
        before = ledger_counts(workspace)
        other = derive(workspace, rows, "other", version="2", inputs=(core.StateInput(label="source", state_id="source"), agencies))
        after = ledger_counts(workspace)
        assert {entity.entity_id for _, entity in workspace.rows(other.state_id)}.isdisjoint(
            urn for urn, _, _ in reference.values())
        # One unit per derive, whatever its size: 400 rows add as many ledger records as 5.
        wide = source_state(workspace, "wide", {f"m{index:03d}": {"n": index} for index in range(400)})
        mark = ledger_counts(workspace)
        derive(workspace, [prepared(key, occurrence) for key, occurrence in wide.items()], "wide", version="3",
               inputs=(core.StateInput(label="source", state_id="wide"), agencies))
        assert {name: count - mark[name] for name, count in ledger_counts(workspace).items()} == {
            name: count - before[name] for name, count in after.items()} == {"records": 8, "retention": 8, "units": 2}


def test_an_incremental_derive_writes_only_changed_rows_and_shares_base_files(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = {key: prepared(key, occurrence) for key, occurrence in occurrences.items()}
        first = derive(workspace, list(rows.values()), "first", dataset="prepared")
        # The source changes a and d, adds f and removes e.
        revised, current = revise(workspace, "source", {"a": {"title": "Amended"}, "d": {"title": "Changed"},
                                                        "f": {"title": "New"}}, removals=("e",))
        # d's prepared value is unchanged but its source occurrence is new; c is supplied again, unchanged.
        changed = {"a": prepared("a", current["a"], title="Amended rule"), "d": prepared("d", current["d"]),
                   "f": prepared("f", current["f"]), "c": prepared("c", current["c"])}
        second = derive(workspace, list(changed.values()), "second", source=revised, base_state_id=first.state_id,
                        removals=("e",), dataset="prepared")
        assert second.report["counts"] == {"rows": 5, "added": 1, "changed": 2, "removed": 1, "unchanged": 1,
                                           "generated": 3, "adopted": 0}
        assert workspace.ledger.current("prepared") == ("state", second.state_id)
        final = {**{key: row for key, row in rows.items() if key != "e"}, **changed}
        reference = expected(final.values())
        with workspace.open_state(first.state_id) as older, workspace.open_state(second.state_id) as newer:
            # At most the source's changes: every member whose row changed, none supplied unchanged.
            assert [(key, urn) for key, urn, _ in newer.changes(older)] == [
                ("a", reference["a"][0]), ("d", reference["d"][0]), ("e", None), ("f", reference["f"][0])]
            assert {row["member_key"]: row["occurrence_id"] for row in typed_rows(newer)} == {
                key: urn for key, (urn, _, _) in reference.items()}
        # Table and membership share the base's files; the new table files hold only the written rows.
        for name in ("table", "membership"):
            base, revision = (set(workspace.records.data_files(layer(workspace, state, name)))
                              for state in (first.state_id, second.state_id))
            assert base < revision
        base_files = set(workspace.records.data_files(layer(workspace, first.state_id, "table")))
        new_files = [str(workspace.records.root / path) for path in
                     workspace.records.data_files(layer(workspace, second.state_id, "table")) if path not in base_files]
        with workspace.records._cursor() as cursor:
            assert cursor.read_parquet(new_files).project("member_key").order("member_key").fetchall() == [
                ("a",), ("d",), ("f",)]
        assert workspace.compare(first.state_id, second.state_id)["counts"] == {"added": 1, "removed": 1, "changed": 2}


@pytest.mark.parametrize("bound", ["delta", "files"])
def test_a_large_delta_or_a_fragmented_membership_is_rewritten_natively(tmp_path, monkeypatch, bound):
    monkeypatch.setattr(core_tables, "DELTA_ROWS" if bound == "delta" else "MEMBERSHIP_FILES", 0 if bound == "delta" else 3)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = {key: prepared(key, occurrence) for key, occurrence in occurrences.items()}
        states = [derive(workspace, list(rows.values()), "first").state_id]
        for index in range(4):
            rows["a"] = {**rows["a"], "title": f"edit {index}"}
            states.append(derive(workspace, [rows["a"]], f"edit-{index}", base_state_id=states[-1]).state_id)
            assert {key: entity.entity_id for key, entity in workspace.rows(states[-1])} == {
                key: urn for key, (urn, _, _) in expected(rows.values()).items()}
        files = [len(workspace.records.data_files(layer(workspace, state, "membership"))) for state in states]
        # Every rewrite leaves one file; small deltas add files only up to the bound.
        assert files == [1] * 5 if bound == "delta" else (max(files) <= 3 and files.count(1) >= 2)


def test_a_point_read_refuses_a_row_its_membership_does_not_name(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = [prepared(key, occurrence) for key, occurrence in occurrences.items()]
        first = derive(workspace, rows, "first")
        second = derive(workspace, [{**rows[0], "title": "Amended"}], "second", base_state_id=first.state_id)
        with workspace.publisher.session() as session:
            old, new = (workspace.states.layers(session, state) for state in (first.state_id, second.state_id))
            # The first state's membership over the second's table: row "a" no longer hashes to its occurrence.
            mixed = StateLayers({**old, "table": new["table"]}, old.identity)
            with workspace.states.relation(session, first.state_id, scope=("c",), layers=mixed) as relation:
                assert [key for key, _, _ in relation.fetchall()] == ["c"]
            with pytest.raises(IntegrityError, match="differs from its minted occurrence"):
                with workspace.states.relation(session, first.state_id, scope=("a", "c"), layers=mixed) as relation:
                    relation.fetchall()


@pytest.mark.parametrize("change", ["rows", "base", "input", "removals", "definition", "schema"])
def test_a_batch_id_retries_exactly_and_refuses_changed_input(tmp_path, change):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        source_state(workspace, "other", {"a": {"title": "elsewhere"}})
        rows = [prepared(key, occurrence) for key, occurrence in occurrences.items()]
        first = derive(workspace, rows[:3], "first")
        options = {"base_state_id": first.state_id, "removals": ("c",)}
        second = derive(workspace, rows[3:], "second", **options)
        files = sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file())
        counts = ledger_counts(workspace)
        # The same rows in another order are the same batch: nothing is written.
        assert derive(workspace, list(reversed(rows[3:])), "second", **options) == second
        assert sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file()) == files
        assert ledger_counts(workspace) == counts
        again = {"rows": rows[3:], "version": "1", "columns": COLUMNS, "schema": SCHEMA, **options}
        if change == "rows":
            again["rows"] = [rows[3], {**rows[4], "title": "changed"}]
        elif change == "base":
            again["base_state_id"] = second.state_id
        elif change == "input":
            again["inputs"] = (core.StateInput(label="source", state_id="other"),)
        elif change == "removals":
            again["removals"] = ("a",)
        elif change == "definition":
            again["version"] = "2"
        else:
            again["schema"] = TableSchema("prepared-test:2", COLUMNS)
        with pytest.raises(IntegrityError, match="batch ID already names different"):
            derive(workspace, again.pop("rows"), "second", **again)
        assert ledger_counts(workspace) == counts


def test_an_incremental_derive_refuses_another_definition_or_schema(tmp_path):
    widened = (*COLUMNS, ("rin", "VARCHAR"))
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = [prepared(key, occurrence) for key, occurrence in occurrences.items()]
        first = derive(workspace, rows, "first", dataset="prepared")
        counts = ledger_counts(workspace)
        for batch_id, options in (("definition", {"version": "2"}),
                                  ("schema", {"columns": widened, "schema": TableSchema(SCHEMA.schema_id, widened)})):
            changed = [{**rows[0], "rin": None}] if batch_id == "schema" else rows[:1]
            with pytest.raises(IntegrityError, match="keeps its base's definition, schema and key"):
                derive(workspace, changed, batch_id, base_state_id=first.state_id, dataset="prepared", **options)
        assert ledger_counts(workspace) == counts and workspace.ledger.current("prepared") == ("state", first.state_id)
        # A new definition derives in full, without a base, as a new identity space.
        rederived = derive(workspace, rows, "rederived", version="2")
        assert rederived.report["counts"]["generated"] == 5


def test_a_stale_dataset_base_refuses_before_writing(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = [prepared(key, occurrence) for key, occurrence in occurrences.items()]
        first = derive(workspace, rows[:2], "first", dataset="prepared")
        derive(workspace, rows[2:3], "second", base_state_id=first.state_id, dataset="prepared")
        counts = ledger_counts(workspace)
        with pytest.raises(StaleBaseError):
            derive(workspace, rows[3:], "stale", base_state_id=first.state_id, dataset="prepared")
        with pytest.raises(StaleBaseError):
            derive(workspace, rows[3:], "fresh", dataset="prepared")
        assert ledger_counts(workspace) == counts


@pytest.mark.parametrize("name,match", [
    ("no-lineage", "source_occurrence_id"), ("reader-column", "occurrence_id"), ("segment-type", "segment_index"),
    ("duplicate", "duplicate member key"), ("null-key", "NULL or empty component"),
    ("removal-put", "removal repeats"), ("removal-without-base", "require a base"), ("empty", "at least one row"),
    ("blob", "unsupported"), ("input-label", "avoid base and rows"),
])
def test_a_refused_derive_writes_nothing(tmp_path, name, match):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = [prepared(key, occurrence) for key, occurrence in occurrences.items()]
        first = derive(workspace, rows[:2], "first")
        counts = ledger_counts(workspace)
        files = sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file())
        options, columns = {}, COLUMNS
        if name == "no-lineage":
            columns = tuple(column for column in COLUMNS if column[0] != "source_occurrence_id")
            rows = [{key: value for key, value in row.items() if key != "source_occurrence_id"} for row in rows]
        elif name == "reader-column":
            columns, rows = (*COLUMNS, ("Occurrence_ID", "VARCHAR")), [{**row, "Occurrence_ID": None} for row in rows]
        elif name == "segment-type":
            columns, rows = (*COLUMNS, ("segment_index", "VARCHAR")), [{**row, "segment_index": "0"} for row in rows]
        elif name == "duplicate":
            rows = [*rows, rows[0]]
        elif name == "null-key":
            rows = [*rows, {**rows[0], "member_key": ""}]
        elif name == "removal-put":
            options = {"base_state_id": first.state_id, "removals": ("a",)}
        elif name == "removal-without-base":
            options = {"removals": ("a",)}
        elif name == "empty":
            rows = []
        elif name == "blob":
            columns, rows = (*COLUMNS, ("hash", "BLOB")), [{**row, "hash": b"x"} for row in rows]
        else:
            options = {"inputs": (core.StateInput(label="rows", state_id="source"),)}
        with pytest.raises(IntegrityError, match=match):
            derive(workspace, rows, "refused", columns=columns, schema=TableSchema(SCHEMA.schema_id, columns), **options)
        assert ledger_counts(workspace) == counts
        assert sorted(path for path in (workspace.path / "records").rglob("*") if path.is_file()) == files


SEGMENTS = (("member_key", "VARCHAR"), ("segment_index", "INTEGER"), ("source_occurrence_id", "VARCHAR"),
            ("text", "VARCHAR"))
SEGMENT_SCHEMA = TableSchema("segments-test:1", SEGMENTS)


def segment(key, index, occurrence, text=None):
    return {"member_key": key, "segment_index": index, "source_occurrence_id": occurrence,
            "text": text or f"{key} part {index}"}


def test_a_one_to_many_layer_replaces_every_row_of_a_changed_source_member(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace, values={"doc#1": {"pages": 3}, "doc": {"pages": 2}, "gone": {"pages": 1}})
        rows = [segment("doc#1", index, occurrences["doc#1"]) for index in range(3)]
        rows += [segment("doc", index, occurrences["doc"]) for index in (1, 12)]
        rows += [segment("gone", 0, occurrences["gone"])]
        first = derive(workspace, rows, "first", columns=SEGMENTS, schema=SEGMENT_SCHEMA)
        keys = sorted(key for key, _ in workspace.rows(first.state_id))
        # member-segment/1 spells member_key#segment_index: doc part 1 and doc#1's parts stay distinct.
        assert keys == ["doc#1", "doc#1#0", "doc#1#1", "doc#1#2", "doc#12", "gone#0"]
        # doc#1 changes to two segments, the second rewritten; "gone" is removed; doc is untouched.
        revised, current = revise(workspace, "source", {"doc#1": {"pages": 2}}, removals=("gone",))
        second = derive(workspace, [segment("doc#1", 0, current["doc#1"]), segment("doc#1", 1, current["doc#1"], "new")],
                        "second", source=revised, columns=SEGMENTS, schema=SEGMENT_SCHEMA, base_state_id=first.state_id,
                        removals=("gone",))
        assert second.report["counts"] == {"rows": 4, "added": 0, "changed": 2, "removed": 2, "unchanged": 0,
                                           "generated": 2, "adopted": 0}
        with workspace.open_state(first.state_id) as older, workspace.open_state(second.state_id) as newer:
            assert [(key, urn is None) for key, urn, _ in newer.changes(older)] == [
                ("doc#1#0", False), ("doc#1#1", False), ("doc#1#2", True), ("gone#0", True)]
            assert newer.read_value("doc#1#1")["text"] == "new"
            assert newer.read_value("doc#1")["text"] == "doc part 1"
            # The typed reader keeps the source member's key; the state's key adds the segment.
            assert [(row["member_key"], row["segment_index"]) for row in typed_rows(newer)] == [
                ("doc", 1), ("doc", 12), ("doc#1", 0), ("doc#1", 1)]


FUSION = (("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR[]"), ("docket", "VARCHAR"), ("title", "VARCHAR"))
FUSION_SCHEMA = TableSchema("fusion-test:1", FUSION)
DOCUMENTS = {"d1": {"docket": "k1"}, "d2": {"docket": "k1"}, "d3": {"docket": "k2"}, "d4": {"docket": None},
             "d5": {"docket": "k3"}}
DOCKETS = {"k1": {"title": "First docket"}, "k2": {"title": "Second docket"}}


def fused(documents, dockets):
    """Join each document to its docket where it exists, carrying the occurrence of every row joined."""
    rows = []
    for key, occurrence in sorted(documents.items()):
        docket = DOCUMENTS[key]["docket"]
        joined = docket in dockets
        rows.append({"member_key": key, "source_occurrence_id": [occurrence, *([dockets[docket]] if joined else [])],
                     "docket": docket, "title": f"{key} in {docket}" if joined else None})
    return rows


def certified_diffs(monkeypatch):
    """Record, for each diff of two states, whether certified revision history narrowed it."""
    certified, original = [], CoreStateStorage.changed_keys

    def spy(self, *args, **kwargs):
        keys = original(self, *args, **kwargs)
        certified.append(keys is not None)
        return keys
    monkeypatch.setattr(CoreStateStorage, "changed_keys", spy)
    return certified


def test_a_fusion_layer_finds_the_rows_a_change_in_either_input_affects(tmp_path, monkeypatch):
    certified = certified_diffs(monkeypatch)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        documents, dockets = source_state(workspace, "documents", DOCUMENTS), source_state(workspace, "dockets", DOCKETS)
        inputs = (core.StateInput(label="documents", state_id="documents"), core.StateInput(label="dockets", state_id="dockets"))
        first = derive(workspace, fused(documents, dockets), "first", columns=FUSION, schema=FUSION_SCHEMA, inputs=inputs)
        # k1 is renamed and k3 added; d3 changes. Revisions certify their edited keys, so the diff reads only those.
        new_dockets, docket_occurrences = revise(workspace, "dockets", {"k1": {"title": "Renamed"}, "k3": {"title": "Third"}})
        new_documents, document_occurrences = revise(workspace, "documents", {"d3": {"docket": "k2", "note": 1}})
        with workspace.open_state(first.state_id) as derived:
            for older, newer, keys in (("dockets", new_dockets, ["d1", "d2"]), ("documents", new_documents, ["d3"]),
                                       ("dockets", "dockets", [])):
                with workspace.open_state(older) as before, workspace.open_state(newer) as after, \
                        derived.affected(before, after) as affected:
                    assert [key for (key,) in affected.project("member_key").order("member_key").fetchall()] == keys
            assert certified == [True, True, True]
            # d5 names no occurrence of the added docket k3, so lineage cannot find it: the caller's join column does.
            with workspace.open_state("dockets") as before, workspace.open_state(new_dockets) as after:
                added = [key for key, occurrence, _ in after.changes(before) if key not in DOCKETS]
            with derived.table() as rows:
                newly_joined = [key for (key,) in rows.filter(f"docket IN ({', '.join(repr(key) for key in added)})")
                                .project("member_key").fetchall()]
            assert added == ["k3"] and newly_joined == ["d5"]
        # Re-derive the affected and newly joined rows over the new inputs.
        refreshed = fused(document_occurrences, docket_occurrences)
        second = derive(workspace, [row for row in refreshed if row["member_key"] in {"d1", "d2", "d3", "d5"}], "second",
                        columns=FUSION, schema=FUSION_SCHEMA, base_state_id=first.state_id,
                        inputs=(core.StateInput(label="documents", state_id=new_documents),
                                core.StateInput(label="dockets", state_id=new_dockets)))
        assert (second.report["counts"]["changed"], second.report["counts"]["unchanged"]) == (4, 0)
        with workspace.open_state(second.state_id) as reader:
            assert {row["member_key"]: row["source_occurrence_id"] for row in typed_rows(reader)} == {
                row["member_key"]: row["source_occurrence_id"] for row in refreshed}


GENERATION = pa.schema([("document_number", pa.string()), ("publication_date", pa.string()), ("title", pa.string())])
TITLES = (("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"), ("title", "VARCHAR"))


def titles(workspace, state_id, keys=None):
    """A derived title row for each row of an admitted generation, its lineage the admitted occurrence."""
    with workspace.open_state(state_id) as reader, reader.table() as relation:
        relation = relation.project("member_key, occurrence_id AS source_occurrence_id, title")
        if keys is not None:
            relation = relation.filter(f"member_key IN ({', '.join(repr(key) for key in keys)})")
        return relation.order("member_key").to_arrow_table().to_pylist()


def test_a_layer_over_admitted_generations_finds_the_rows_the_next_generation_changed(tmp_path, monkeypatch):
    certified = certified_diffs(monkeypatch)
    rows = [{"document_number": f"2026-{index}", "publication_date": "2026-09-25", "title": f"Rule {index}"}
            for index in range(4)]
    later = [{**rows[0], "title": "Amended rule"}, rows[1], rows[2],
             {"document_number": "2026-9", "publication_date": "2026-09-26", "title": "New rule"}]
    for name, values in (("g1", rows), ("g2", later)):
        generation(tmp_path / name, pa.Table.from_pylist(values, schema=GENERATION))
    schema = TableSchema("titles-test:1", TITLES)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        first = workspace.admit_generation(tmp_path / "g1", family="federal-register", table="federal_register", dataset="fr")
        derived = derive(workspace, titles(workspace, first.state_id), "titles", columns=TITLES, schema=schema,
                         source=first.state_id)
        second = workspace.admit_generation(tmp_path / "g2", family="federal-register", table="federal_register", dataset="fr")
        with workspace.open_state(first.state_id) as older, workspace.open_state(second.state_id) as newer:
            changes = {key: occurrence for key, occurrence, _ in newer.changes(older)}
            with workspace.open_state(derived.state_id) as layer, layer.affected(older, newer) as affected:
                found = sorted(affected.project("member_key, source_occurrence_id").fetchall())
        # Table-shaped inputs record no revisions: one pass over both memberships finds the changed and removed rows.
        assert certified == [False, False]
        assert changes.keys() == {"2026-0@2026-09-25", "2026-3@2026-09-25", "2026-9@2026-09-26"}
        assert found == [(row["member_key"], row["source_occurrence_id"]) for row in titles(workspace, first.state_id)
                         if row["member_key"] in {"2026-0@2026-09-25", "2026-3@2026-09-25"}]
        revised = derive(workspace, titles(workspace, second.state_id, [key for key, occurrence in changes.items() if occurrence]),
                         "titles-2", columns=TITLES, schema=schema, source=second.state_id, base_state_id=derived.state_id,
                         removals=tuple(key for key, occurrence in changes.items() if occurrence is None))
        assert revised.report["counts"] == {"rows": 4, "added": 1, "changed": 1, "removed": 1, "unchanged": 0,
                                            "generated": 2, "adopted": 0}
        with workspace.open_state(revised.state_id) as reader:
            # Unchanged rows kept the admitted occurrences the next generation carried forward.
            assert [{name: row[name] for name, _ in TITLES} for row in typed_rows(reader)] == titles(workspace, second.state_id)
        assert [item.state_id for item in workspace.generating_request(revised.state_id).inputs[:1]] == [second.state_id]


def test_removing_an_older_derived_state_frees_only_files_no_retained_layer_references(tmp_path):
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        occurrences = source_state(workspace)
        rows = {key: prepared(key, occurrence) for key, occurrence in occurrences.items()}
        first = derive(workspace, list(rows.values()), "first", dataset="prepared")
        second = derive(workspace, [{**rows["a"], "title": "Amended"}], "second", base_state_id=first.state_id,
                        dataset="prepared")
        final = {**rows, "a": {**rows["a"], "title": "Amended"}}
        with workspace.publisher.session() as session:
            layers = {state: workspace.states.layers(session, state) for state in (first.state_id, second.state_id)}
        referenced = {state: {ref.locator for layer_ in state_layers.values()
                              for ref in workspace.records.physical_references(layer_.reference)}
                      for state, state_layers in layers.items()}
        shared, own = referenced[first.state_id] & referenced[second.state_id], referenced[first.state_id] - referenced[second.state_id]
        assert shared and own
        # The newer state's request binds its sources, never its base: the older state is removable.
        keys = [("result", first.state_id + ":execution:result"), ("state", first.state_id),
                ("state_representation", first.state_id + ":physical")]
        policy = core.RetentionPolicy(format_version=1, policy_id="superseded",
                                      description={"remove": [list(key) for key in keys], "collect_unreferenced": False})
        workspace.retain([policy], unit_id="superseded", roots=[("retention_policy", "superseded")])
        outcome = workspace.maintenance.remove_under_policy("remove-first", "superseded", keys)
        assert outcome["deleted"] > 0
        root = workspace.path / "records"
        assert not any((root / locator).exists() for locator in own)
        assert all((root / locator).exists() for locator in shared)
        with workspace.open_state(second.state_id) as reader:
            assert {key: value for key, _, value in reader.values()} == {
                key: value for key, (_, _, value) in expected(final.values()).items()}
            assert len(typed_rows(reader)) == 5
