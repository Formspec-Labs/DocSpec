"""One typed native extraction path for Core values and the capacity probe."""

from contextlib import closing

import duckdb
import msgspec

from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.core_encoding import ABSENT, member_bytes, selected_fields
from docspec.domain.identity import canonical_value_bytes
from docspec.errors import IntegrityError
from docspec.ports.record_storage import BATCH_ROWS


_ENGINE_JSON = msgspec.json.Decoder()


def field_paths(fields):
    """Admit the definition once before opening or querying any source."""
    selector = core.Whole() if fields is None else core.JsonFields(selectors=tuple(core.Field(label=label, pointer=pointer) for label, pointer in fields))
    record_value(selector, core.Selector)
    return [""] if fields is None else [pointer for _, pointer in fields]


def extracted_rows(relation, *, fields=None, sort_fields=(), read_rows=256, metrics=None, passthrough=()):
    """Yield key, occurrence and typed selection from admitted JSON source rows.

    The source has member_key, occurrence_id and payload columns. A missing
    occurrence marks a missing named member. Engine-produced JSON is decoded
    here; the shared encoder remains the only owner of comparison bytes.
    """
    paths = field_paths(fields)
    selected_count = len(paths)
    paths += field_paths([(str(index), pointer) for index, pointer in enumerate(sort_fields)])
    if read_rows < 1 or read_rows > BATCH_ROWS:
        raise ValueError("read_rows must be between 1 and 2048")
    metrics = {} if metrics is None else metrics
    for name in ("engine_json_decodes", "shared_encoder_calls", "converted_rows", "selected_json_bytes", "largest_arrow_batch_bytes", "sql_calls"):
        metrics.setdefault(name, 0)
    metrics["sql_calls"] += 1
    column, constant, function = duckdb.ColumnExpression, duckdb.ConstantExpression, duckdb.FunctionExpression
    query = relation.project(column("member_key"), column("occurrence_id"),
                             function("json_extract", column("payload"), constant(paths)).alias("selected"),
                             function("json_type", column("payload"), constant(paths)).alias("types"),
                             *(column(name) for name in passthrough))
    with closing(query.to_arrow_reader(read_rows)) as reader:
        for batch in reader:
            metrics["largest_arrow_batch_bytes"] = max(metrics["largest_arrow_batch_bytes"], batch.nbytes)
            for key, entity, selections, types, *extra in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                if entity is None:
                    value = ABSENT
                else:
                    if selections is None:
                        raise IntegrityError("present occurrence has no JSON value")
                    extracted = []
                    for data, kind in zip(selections, types, strict=True):
                        if kind is None:
                            extracted.append(ABSENT)
                        else:
                            metrics["engine_json_decodes"] += 1
                            metrics["selected_json_bytes"] += len(data.encode("utf-8"))
                            extracted.append(_ENGINE_JSON.decode(data))
                    selected = extracted[:selected_count]
                    value = ["present", selected[0]] if fields is None else selected_fields(
                        (label, item) for (label, _), item in zip(fields, selected, strict=True))
                sort_key = canonical_value_bytes([["absent"] if item is ABSENT else ["present", item]
                                                  for item in extracted[selected_count:]]).decode() if sort_fields and entity is not None else "[]"
                metrics["converted_rows"] += 1
                yield key, entity, value, sort_key, *extra


def encoded_members(relation, *, fields=None, material_keys=False, material_entities=False, read_rows=256, metrics=None):
    """Encode admitted rows into comparison member bytes through the shared encoder."""
    metrics = {} if metrics is None else metrics
    with closing(extracted_rows(relation, fields=fields, read_rows=read_rows, metrics=metrics)) as rows:
        for key, entity, value, _ in rows:
            encoded = member_bytes(value, member_key=key if material_keys else None, entity_id=entity if material_entities else None)
            metrics["shared_encoder_calls"] += 1
            yield key, entity, encoded
