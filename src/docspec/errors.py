"""Stable exception hierarchy for DocSpec domain and adapter boundaries."""


class DocSpecError(Exception):
    """Base class for a rejected DocSpec operation."""


class IntegrityError(DocSpecError):
    """Bytes, identity, membership, or schema failed closed verification."""


class StateTransitionError(DocSpecError):
    """A requested immutable state transition is not allowed."""


class ProfileError(DocSpecError):
    """A storage or processing profile is invalid or incompatible."""


class LimitExceededError(DocSpecError):
    """Input or work exceeded a sealed resource limit."""


class StaleBaseError(DocSpecError):
    """A catalog commit no longer matches its expected base release."""
