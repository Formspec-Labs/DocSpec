"""Protected original-member and selected-result joins without new data files."""

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from docspec.application.core_dependencies import CoreDependencies, corresponds
from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.identity import OrderedJsonSequenceDigester, canonical_value_bytes, require_text, sha256_digest
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError, StateValueRelationUnavailable
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS
from docspec.runtime.state_reader import CoreStateReader


@dataclass(frozen=True, slots=True)
class SelectedOutput:
    """One selected operation output with its origin, result, entity and pin identities."""

    origin: core.Origin
    selection_id: str
    result_id: str
    output_id: str
    output_pin: str
    label: str
    value: Any


class SelectedOutputReader:
    """Use through CoreWorkspace.open_selected_outputs.

    All matching retained selections remain visible, including competing
    choices. The consumer decides whether duplicates can be used together.
    ``source`` is the public, already-admitted CoreStateReader for original
    values and native relations; reuse it within this same protected context.
    Missing choices yield no rows. Missing retained evidence, stale dependency
    evidence and opaque bytes respectively refuse as IntegrityError,
    StaleBaseError and StateValueRelationUnavailable. Rows are provisional until
    iteration completes. Discovery reads bounded selection metadata, not every
    retained source value; input and result content use their existing bounds.
    """

    def __init__(self, session, states, state_id, *, definition_id, output_labels, expected_state_pin=None, expected_pin=None,
                 selection_ids=None):
        require_text(definition_id, "operation definition identity")
        if (not isinstance(output_labels, tuple) or not output_labels or len(output_labels) > BATCH_ROWS
                or any(not isinstance(label, str) or not label for label in output_labels)
                or len(set(output_labels)) != len(output_labels)):
            raise ValueError("output labels require a bounded nonempty tuple of distinct names")
        if selection_ids is not None and (
                not isinstance(selection_ids, tuple) or not 0 < len(selection_ids) <= BATCH_ROWS
                or any(not isinstance(key, str) or not key for key in selection_ids)
                or len(set(selection_ids)) != len(selection_ids)):
            raise ValueError("selection IDs require a bounded nonempty tuple of distinct names")
        if selection_ids is not None and (sum(map(len, selection_ids)) > BATCH_BYTES
                or len(canonical_value_bytes(selection_ids)) > BATCH_BYTES):
            raise ValueError("selection IDs exceed the batch byte limit")
        self.selection_ids = None if selection_ids is None else tuple(sorted(selection_ids))
        self._session, self._states = session, states
        self.source = CoreStateReader(session, states, state_id, expected_pin=expected_state_pin)
        self.state_id, self.state_pin = state_id, self.source.pin
        self.definition_id, self.output_labels = definition_id, output_labels
        self.definition = self._read(("operation_definition", definition_id))
        self.definition_pin = sha256_digest(canonical_value_bytes(record_value(self.definition)))
        self._expected_pin, self._pin = expected_pin, None

    @property
    def pin(self):
        """Exact source, definition and selected identities after a complete read."""
        if self._pin is None:
            raise StateTransitionError("selected output pin requires a completed traversal")
        return self._pin

    def _read(self, key):
        return self._stored(key).value

    def _stored(self, key, *, require_available=True):
        with owned_iterator(self._session.read_records([key])) as batches:
            row = next(batches)[0]
        if (row is None or not row.retained or (require_available and not row.available)
                or row.value is None or row.row_digest is None):
            raise IntegrityError("selected output requires retained available " + key[0])
        return row

    def _selections(self):
        rows = (self._session.ledger.retained_records(kind="selection") if self.selection_ids is None else
                self._session.read_records(("selection", key) for key in self.selection_ids))
        with owned_iterator(rows) as batches:
            for batch in batches:
                self._session._active()
                selected, size = [], 0
                for row in batch:
                    if row is None or row.value is None or not row.retained:
                        raise IntegrityError("retained selection description is unavailable")
                    selection = row.value
                    if selection.target.state_id != self.state_id:
                        if self.selection_ids is not None:
                            raise IntegrityError("explicit selected output belongs to another source state")
                        continue
                    request_row = self._stored(("request", selection.request_id), require_available=False)
                    request = request_row.value
                    if request.definition_id != self.definition_id:
                        if self.selection_ids is not None:
                            raise IntegrityError("explicit selected output belongs to another definition")
                        continue
                    labels = tuple(label for label in self.output_labels if label in selection.output_labels)
                    if not labels and self.selection_ids is not None:
                        raise IntegrityError("explicit selected output has none of the requested labels")
                    if labels:
                        if not row.available or not request_row.available:
                            raise IntegrityError("selected output selection or request is unavailable")
                        row_size = len(canonical_value_bytes(record_value(selection))) + len(canonical_value_bytes(record_value(request)))
                        if row_size > BATCH_BYTES:
                            raise IntegrityError("selected output description exceeds the batch byte limit")
                        if selected and size + row_size > BATCH_BYTES:
                            yield selected
                            selected, size = [], 0
                        selected.append((selection, request, labels, row.row_digest))
                        size += row_size
                if selected:
                    yield selected

    def _check_members(self, selections):
        keys = tuple(dict.fromkeys(selection.target.member_key for selection, _, _, _ in selections))
        if any(key is None for key in keys):
            raise IntegrityError("selected output origin omits its source member key")
        # Reuse already-admitted immutable layers. Only this bounded set of
        # member addresses is read, never a new full-state admission per result.
        with self._states.relation(self._session, self.state_id, scope=keys, layers=self.source._layers) as relation:
            actual = dict(relation.project("member_key, occurrence_id").fetchall())
        for selection, _, _, _ in selections:
            if actual.get(selection.target.member_key) != selection.target.parent_entity_id:
                raise StaleBaseError("selected output origin differs from the source member occurrence")

    def rows(self):
        """Yield each selected output, then set the selection-set pin for a complete traversal."""

        self._session._active()
        self._pin = None
        prefix = ("docspec-selected-outputs", 1, self.state_pin, self.definition_pin, list(self.output_labels))
        if self.selection_ids is not None:
            prefix += (list(self.selection_ids),)
        digester = OrderedJsonSequenceDigester(prefix=prefix)
        dependencies = CoreDependencies()
        with closing(self._selections()) as groups:
            for group in groups:
                self._check_members(group)
                for selection, request, labels, selection_pin in group:
                    self._session._active()
                    self._session.validate(MetadataBatch("read:" + selection.selection_id,
                        retained=(("selection", selection.selection_id),)))
                    current = dependencies.assess(self._session, request, self.definition)
                    prior = dependencies.assess_result(self._session, selection.selected_result_id)
                    if not corresponds(current, prior):
                        raise StaleBaseError("selected output dependency evidence no longer corresponds")
                    result_row = self._stored(("result", selection.selected_result_id))
                    result = result_row.value
                    outputs = {binding.label: binding for binding in result.outcome.outputs}
                    for label in labels:
                        if label not in outputs:
                            raise IntegrityError("selected output label is absent from its result")
                        binding = outputs[label]
                        entity_row = self._stored(("entity", binding.entity_id))
                        entity = entity_row.value
                        if isinstance(entity.value, core.ContentRef) and entity.value.codec != "json-v1":
                            raise StateValueRelationUnavailable("selected output contains opaque bytes; select JSON values")
                        value = self.source._read_value(entity)
                        digester.accept_admitted_payload(canonical_value_bytes([
                            record_value(selection.target, core.Origin), selection.selection_id, selection_pin, result.result_id,
                            result_row.row_digest, entity.entity_id, entity_row.row_digest, label]))
                        yield SelectedOutput(selection.target, selection.selection_id, result.result_id,
                                             entity.entity_id, entity_row.row_digest, label, value)
        pin = digester.finish()
        if self._expected_pin is not None and pin != self._expected_pin:
            raise StaleBaseError("selected outputs differ from the expected selection-set pin")
        self._pin = pin
