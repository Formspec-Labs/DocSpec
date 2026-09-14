"""C03 candidates against independent selected values and exact digest bytes."""

from collections import Counter
import hashlib
import json
from pathlib import Path

from hypothesis import given, settings
import pyarrow.parquet as pq
import pytest

from docspec.adapters.storage.batches import BATCH_BYTES
from docspec.domain.identity import canonical_value_bytes
from docspec.errors import IntegrityError, LimitExceededError
from tests.support.core_bulk_experiment import (
    SELECTED_SCHEMA, SOURCE_SCHEMA, connection, framed_digest, selected_rows, write_rows,
)
from tests.support.core_reference import State, selected_field, value_key
from tests.support.core_strategies import json_values


FIXTURES = json.loads((Path(__file__).parent / "fixtures/core/selected-values.json").read_text())


def source(path, rows):
    return write_rows(path, (
        (key, entity, canonical_value_bytes(value).decode()) for key, entity, value in rows
    ), SOURCE_SCHEMA, byte_column=2)


@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: case["name"])
def test_sql_extraction_and_canonical_bytes_match_known_answers(tmp_path, case):
    path = tmp_path / "source.parquet"
    source(path, [("k", "e", case["value"])])
    with connection(tmp_path / "scratch") as con:
        if "error" in case:
            with pytest.raises(IntegrityError, match="invalid JSON Pointer"):
                list(selected_rows(con, path, fields=[("v", case["pointer"])]))
        else:
            actual = list(selected_rows(con, path, fields=[("v", case["pointer"])]))
            assert actual == [(canonical_value_bytes(["present", [["v", *case["expected"]]]]),)]


@given(json_values)
@settings(max_examples=80, deadline=None)
def test_sql_preserves_the_whole_admitted_json_domain(value):
    # A native relation exercises generated values without a temporary file per
    # Hypothesis example. Expected logical types come from the independent oracle.
    import duckdb
    import msgspec
    from docspec.domain.core_admission import pointer_tokens

    with duckdb.connect(config={"threads": 1}) as con:
        payload = canonical_value_bytes({"x": value}).decode()
        pointer_tokens("/x")
        result = con.execute("SELECT json_extract(?, '/x')::VARCHAR", [payload]).fetchone()[0]
    decoded = msgspec.json.decode(result)
    assert value_key(decoded) == value_key(value)
    assert canonical_value_bytes(decoded) == canonical_value_bytes(value)


def test_named_scope_multiset_materiality_and_chunk_sizes(tmp_path):
    rows = [("a", "e1", {}), ("b", "e2", {"x": None}), ("c", "e3", {"x": 1}), ("d", "e4", {"x": 1})]
    path = tmp_path / "source.parquet"
    source(path, rows)
    state = State.root("s", rows)
    with connection(tmp_path / "scratch") as con:
        for index, (scope, keys, entities) in enumerate([
            (None, False, False), (["missing", "a", "b", "d", "c"], True, True),
            (["d", "c"], False, False), ([], False, False),
        ]):
            expected = state.selected_members(
                selectors=[{"label": "field", "pointer": "/x"}], scope=scope,
                material_keys=keys, material_entities=entities,
            )
            metrics = {}
            actual = list(selected_rows(con, path, fields=[("field", "/x")], scope=scope,
                                        material_keys=keys, material_entities=entities, metrics=metrics))
            assert Counter(value_key(json.loads(row[0])) for row in actual) == Counter(map(value_key, expected))
            assert metrics["shared_encoder_calls"] == len(expected)
            encoded = sorted(canonical_value_bytes(row) for row in expected)
            preimage = b'["docspec-selected-members",1,[' + b",".join(encoded) + b"]]"
            expected_digest = "sha256:" + hashlib.sha256(preimage).hexdigest()
            output = tmp_path / f"selected-{index}.parquet"
            write_rows(output, actual, SELECTED_SCHEMA, byte_column=0)
            for chunk in (1, 3, 256):
                assert framed_digest(con, output, read_rows=chunk)["digest"] == expected_digest


def test_whole_state_retains_keys_and_duplicate_occurrences(tmp_path):
    path = tmp_path / "source.parquet"
    rows = [("a", "e1", {"x": 1}), ("b", "e2", {"x": 1})]
    source(path, rows)
    with connection(tmp_path / "scratch") as con:
        actual = list(selected_rows(con, path, fields=None, material_keys=True))
    expected = State.root("s", rows).selected_members(material_keys=True)
    assert Counter(value_key(json.loads(row[0])) for row in actual) == Counter(map(value_key, expected))


def test_full_workload_oracle_orders_complete_values_not_just_prefixes():
    from tests.support.core_bulk_expected import expected_rows
    from tests.support.core_workload import core_members

    rows = list(core_members(2050))
    state = State.root("s", rows)
    expected = sorted(canonical_value_bytes(row) for row in state.selected_members(material_keys=True))
    assert list(expected_rows("whole", len(rows))) == expected


def test_duplicate_scope_and_selector_labels_refuse(tmp_path):
    with connection(tmp_path / "scratch") as con:
        with pytest.raises(IntegrityError, match="duplicate requested"):
            list(selected_rows(con, tmp_path / "unused", scope=["a", "a"]))
        with pytest.raises(IntegrityError, match="duplicate selector"):
            list(selected_rows(con, tmp_path / "unused", fields=[("a", "/x"), ("a", "/y")]))


def test_handoffs_are_bounded_by_bytes_as_well_as_rows(tmp_path):
    payload = b"x" * (BATCH_BYTES // 2)
    path = tmp_path / "bytes.parquet"
    result = write_rows(path, [(payload,)] * 5, SELECTED_SCHEMA, byte_column=0)
    assert result["largest_write_batch_bytes"] == BATCH_BYTES
    assert pq.ParquetFile(path).metadata.num_row_groups == 3
    with pytest.raises(LimitExceededError):
        write_rows(tmp_path / "oversized.parquet", [(b"x" * (BATCH_BYTES + 1),)], SELECTED_SCHEMA, byte_column=0)


@pytest.mark.parametrize("token", ["", "~", "/", "a\x00b", '"', "\\", "01", "-1", "٩", "9" * 5000])
def test_pointer_object_tokens_are_not_treated_as_array_indices(tmp_path, token):
    pointer = "/" + token.replace("~", "~0").replace("/", "~1")
    path = tmp_path / "source.parquet"
    value = {token: "found"}
    source(path, [("key", "entity", value)])
    with connection(tmp_path / "scratch") as con:
        actual = list(selected_rows(con, path, fields=[("v", pointer)]))
    assert actual == [(canonical_value_bytes(["present", [["v", *selected_field(value, pointer)]]]),)]
