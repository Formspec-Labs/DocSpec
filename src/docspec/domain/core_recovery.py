"""Closed recovery documents shared by continuation, publication and retention.

``recovery_document`` enforces each kind's exact field set, version, and execution identity before use.
"""

from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.identity import require_text
from docspec.errors import IntegrityError


_FIELDS = {
    "checkpoint": {"format", "version", "execution_id", "partial_result_id", "record_keys", "roots", "state", "selection"},
    "publication": {"format", "version", "unit_id", "executions", "records", "roots"},
}


def recovery_document(value, kind, execution_id):
    """Validate the recovery document an execution owner can actually resume.

    Refuses an unknown kind, a wrong closed shape or version, and an execution identity that does not match.
    """

    if (kind not in _FIELDS or not isinstance(value, dict) or set(value) != _FIELDS[kind]
            or value["format"] != "docspec-operation-" + kind or type(value["version"]) is not int or value["version"] != 1):
        raise IntegrityError("invalid operation " + kind + " document")
    key_fields = ["roots"]
    if kind == "checkpoint":
        if value["execution_id"] != execution_id:
            raise IntegrityError("checkpoint identifies another execution")
        require_text(value["partial_result_id"], "checkpoint result identity")
        key_fields.append("record_keys")
    else:
        require_text(value["unit_id"], "publication unit identity")
        if not isinstance(value["executions"], list) or execution_id not in value["executions"]:
            raise IntegrityError("publication journal does not identify its execution")
        if not isinstance(value["records"], list):
            raise IntegrityError("publication journal requires Core records")
        for record in value["records"]:
            record_value(record)
    for field in key_fields:
        keys = value[field]
        if not isinstance(keys, list) or any(not isinstance(key, list) or len(key) != 2
                or not isinstance(key[0], str) or key[0] not in core.RECORD_ID_FIELDS
                or not isinstance(key[1], str) or not key[1] for key in keys):
            raise IntegrityError("recovery document has invalid " + field)
    return value
