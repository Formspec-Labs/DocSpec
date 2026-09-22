"""Canonical Core entity rows shared by state, ledger and export writers."""

from contextlib import closing

from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.streams import owned_iterator
from docspec.domain.core import Entity
from docspec.domain.core_admission import record_parts
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError


ENTITY_SCHEMA = RecordSchema("core-entities:1", ("kind", *Entity.__struct_fields__), "entity_id", "entity_id")
ENTITY_POLICY = PartitionPolicy("core-values:1", 1)


def retain_entities(storage, records):
    """Admit entity records, then write their exact canonical bytes."""
    def payloads():
        with owned_iterator(records) as source:
            for record in source:
                value, payload = record_parts(record)
                if value["kind"] != "entity":
                    raise IntegrityError("entity record layer requires entity-only rows")
                yield value["entity_id"], payload

    return retain_entity_payloads(storage, payloads())


def retain_entity_payloads(storage, payloads):
    """Write (entity ID, already admitted canonical bytes) pairs without decoding them again."""
    def rows():
        with owned_iterator(payloads) as source:
            for entity_id, payload in source:
                yield entity_id, entity_id, payload

    with closing(encoded_batches(rows(), ENCODED_RECORD_SCHEMA, byte_column=2)) as batches:
        return storage.retain_batches(batches, layer_kind="core-entities", schema=ENTITY_SCHEMA,
                                      partition_policy=ENTITY_POLICY, ordered=False)
