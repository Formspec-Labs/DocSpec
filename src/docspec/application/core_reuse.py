"""Candidate policy and exact association, using the common dependency owner."""

from collections.abc import Callable
from dataclasses import dataclass

from docspec.application.core_dependencies import CoreDependencies, corresponds
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS


@dataclass(frozen=True, slots=True)
class ReuseRequest:
    """One candidate request to reuse, with its selection identity, target and eligibility policy."""
    definition: core.OperationDefinition
    request: core.Request
    selection_id: str
    target: core.Origin
    policy: Callable[[core.Result], bool]
    output_labels: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """A published association selection together with the result it reuses."""
    selection: core.Selection
    result: core.Result


def selection_for(result, request_id, *, selection_id, target, output_labels=None):
    """Build an admitted Selection binding one result to its request and target."""
    return admit_record(encode_record(core.Selection(format_version=1, selection_id=selection_id, target=target,
        request_id=request_id, selected_result_id=result.result_id,
        output_labels=tuple(output.label for output in result.outcome.outputs) if output_labels is None else tuple(output_labels))))


class CoreReuse:
    """Choose reusable results and publish exact associations through the dependency owner."""
    def __init__(self):
        self.dependencies = CoreDependencies()

    @staticmethod
    def _record(session, key):
        with owned_iterator(session.read_records([key])) as batches:
            return next(batches)[0]

    def existing(self, session, call):
        """Recover the particular prior choice; never rerun today's policy."""
        return self.existing_many(session, (call,))[0]

    def existing_many(self, session, calls):
        """Read exact prior choices in batches, retaining per-choice closure bounds."""
        calls = bounded_items(calls, limit=BATCH_ROWS)
        metadata_bytes = 0
        def records(keys):
            nonlocal metadata_bytes
            found = {}
            keys = tuple(dict.fromkeys(keys))
            if not keys:
                return found
            with owned_iterator(session.read_records(keys)) as batches:
                for batch in batches:
                    for row in batch:
                        if row is None:
                            continue
                        metadata_bytes += len(encode_record(row.value)) if row.value is not None else 0
                        if metadata_bytes > BATCH_BYTES:
                            raise LimitExceededError("exact reuse group exceeds its metadata byte limit")
                        found[row.key] = row
            return found
        selections = records(("selection", call.selection_id) for call in calls)
        present = tuple(call for call in calls if ("selection", call.selection_id) in selections)
        descriptions = records(key for call in present for key in (
            ("request", call.request.request_id), ("operation_definition", call.definition.definition_id)))
        for call in present:
            selection = selections[("selection", call.selection_id)].value
            if (selection.request_id != call.request.request_id or selection.target != call.target
                    or (call.output_labels is not None and selection.output_labels != call.output_labels)):
                raise IntegrityError("selection identity already describes another request or target")
            for key, expected in ((("request", call.request.request_id), call.request),
                                  (("operation_definition", call.definition.definition_id), call.definition)):
                stored = descriptions.get(key)
                if stored is None or encode_record(stored.value) != encode_record(expected):
                    raise IntegrityError("selection retry differs from its original request or definition")
            # Recheck availability through the publisher's live ledger reads.
            # Separate closures preserve the existing per-operation byte bound.
            session.validate(MetadataBatch("read:" + call.selection_id, retained=(("selection", call.selection_id),)))
        results = records(("result", row.value.selected_result_id) for row in selections.values())
        choices = []
        for call in calls:
            stored = selections.get(("selection", call.selection_id))
            if stored is None:
                choices.append(None)
                continue
            result = results.get(("result", stored.value.selected_result_id))
            if result is None or not result.retained or not result.available or result.value is None:
                raise IntegrityError("exact selection result is unavailable")
            choices.append(Resolution(stored.value, result.value))
        return tuple(choices)

    @staticmethod
    def selection(call, result):
        """Build the association record for one chosen result."""
        return selection_for(result, call.request.request_id, selection_id=call.selection_id,
                             target=call.target, output_labels=call.output_labels)

    def choose_many(self, session, calls):
        """Join one bounded group of requests to every matching candidate.

        Candidate IDs stream from SQL; one chosen result per request is retained.
        The publisher rechecks correspondence, availability and evidence guards.
        """
        session._active()
        calls = bounded_items(calls, limit=BATCH_ROWS)
        size = 0
        prepared, assessments, choices = [], {}, [None] * len(calls)
        for ordinal, call in enumerate(calls):
            definition, request = encode_record(call.definition), encode_record(call.request)
            size += len(definition) + len(request)
            if size > BATCH_BYTES:
                raise LimitExceededError("reuse request group exceeds its metadata byte limit")
            call = ReuseRequest(admit_record(definition), admit_record(request), call.selection_id, call.target, call.policy, call.output_labels)
            prepared.append(call)
        choices = list(self.existing_many(session, prepared))
        for ordinal, call in enumerate(prepared):
            if choices[ordinal] is None:
                assessment = self.dependencies.assess(session, call.request, call.definition)
                if assessment.adequate and assessment.digest is not None:
                    assessments[ordinal] = assessment
        wanted = ((str(ordinal), assessment.digest) for ordinal, assessment in assessments.items())
        with owned_iterator(session.ledger.find_candidates(wanted)) as batches:
            for batch in batches:
                for candidate in batch:
                    ordinal = int(candidate.request_id)
                    if choices[ordinal] is not None:
                        continue
                    call = prepared[ordinal]
                    try:
                        prior = self.dependencies.assess_result(session, candidate.result_id)
                        if not corresponds(assessments[ordinal], prior):
                            continue
                        result = self._record(session, ("result", candidate.result_id)).value
                    except (IntegrityError, StaleBaseError):
                        continue
                    # Policy sees only an eligible result and cannot establish
                    # adequacy itself. A failed policy callback remains visible.
                    if call.policy(result) is not True:
                        continue
                    selection = self.selection(call, result)
                    try:
                        session.publish(MetadataBatch("selection:" + call.selection_id,
                            records=(call.definition, call.request, selection), retained=(("selection", call.selection_id),),
                            expected_versions=((("result", result.result_id), candidate.evidence_version),)))
                    except (IntegrityError, StaleBaseError):
                        continue
                    choices[ordinal] = Resolution(selection, result)
        return tuple(choices)
