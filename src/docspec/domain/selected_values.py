"""Closed result shapes for labeled field selections."""

from docspec.errors import IntegrityError


def validate_fields_value(selectors, value):
    """Validate framing without coercing or reinterpreting selected JSON values."""
    if not isinstance(value, list) or len(value) != len(selectors):
        raise IntegrityError("selected fields differ from their definition")
    for field, item in zip(selectors, value, strict=True):
        if (not isinstance(item, list) or len(item) not in (2, 3) or item[0] != field.label
                or (item[1] == "absent" and len(item) != 2) or (item[1] == "present" and len(item) != 3)
                or item[1] not in ("absent", "present")):
            raise IntegrityError("selected field has invalid label or presence framing")
