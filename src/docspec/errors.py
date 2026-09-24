"""Stable exception hierarchy for DocSpec domain and adapter boundaries."""


class DocSpecError(Exception):
    """Base class for a rejected DocSpec operation."""


class IntegrityError(DocSpecError):
    """Bytes, identity, membership, or schema failed closed verification."""


class SchemaValidationError(IntegrityError):
    """Keep machine-readable schema locations alongside a useful message."""

    def __init__(
        self, label: str, message: str, *,
        instance_path: tuple[str | int, ...], schema_path: tuple[str | int, ...],
    ) -> None:
        self.instance_path = instance_path
        self.schema_path = schema_path
        self.message = message
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in instance_path)
        super().__init__(f"{label} schema failure at {pointer if instance_path else '$'}: {message}")


class StateTransitionError(DocSpecError):
    """A requested immutable state transition is not allowed."""


class StateValueRelationUnavailable(DocSpecError):
    """A state needs the decoded value stream because some values are external."""


class ProfileError(DocSpecError):
    """A storage or processing profile is invalid or incompatible."""


class LimitExceededError(DocSpecError):
    """Input or work exceeded a sealed resource limit."""


class StaleBaseError(DocSpecError):
    """A catalog commit no longer matches its expected base release."""


class IdentitiesChangedError(StaleBaseError):
    """Bulk identities changed between a publication's identity checks and its commit; checking again may succeed."""
