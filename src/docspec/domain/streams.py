"""Shared lifetime ownership for bounded application and adapter streams."""

from contextlib import contextmanager

from docspec.errors import LimitExceededError


@contextmanager
def owned_iterator(values):
    """Close a producer exactly once, including native callback failures."""
    source = iter(values)
    try:
        yield source
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def bounded_items(values, *, limit):
    """Materialize one bounded control unit while retaining producer ownership."""
    result = []
    with owned_iterator(values) as source:
        for value in source:
            if len(result) == limit:
                raise LimitExceededError(f"control unit exceeds {limit} rows")
            result.append(value)
    return tuple(result)

