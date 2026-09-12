"""Small comparison primitives shared by catalog and saved-result inspection."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

from docspec.domain.identity import canonical_json_bytes

_MISSING = object()


def json_equal(older: Any, newer: Any) -> bool:
    """Compare admitted JSON values, where true is distinct from the number 1."""

    return canonical_json_bytes(older) == canonical_json_bytes(newer)


def json_changes(older: Any, newer: Any, path: str = "") -> list[dict[str, Any]]:
    """Describe changed JSON fields without conflating missing keys with null."""

    if older is _MISSING or newer is _MISSING:
        return [{
            "path": path, "older": None if older is _MISSING else older,
            "newer": None if newer is _MISSING else newer,
            "olderPresent": older is not _MISSING, "newerPresent": newer is not _MISSING,
        }]
    if json_equal(older, newer):
        return []
    if isinstance(older, dict) and isinstance(newer, dict):
        return [
            change
            for key in sorted(older.keys() | newer.keys())
            for change in json_changes(
                older.get(key, _MISSING), newer.get(key, _MISSING),
                f"{path}/{key.replace('~', '~0').replace('/', '~1')}",
            )
        ]
    return [{"path": path, "older": older, "newer": newer}]


def paired_rows(
    older: Iterator[dict[str, Any]],
    newer: Iterator[dict[str, Any]],
    *,
    key: Callable[[dict[str, Any]], Any],
) -> Iterator[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Merge two uniquely ordered streams, retaining at most one row from each.

    Callers supply their existing storage order and close a partially consumed
    result. Closing or failing the merge closes both source iterators.
    """

    with ExitStack() as stack:
        for rows in (older, newer):
            close = getattr(rows, "close", None)
            if close is not None:
                stack.callback(close)
        old, new = next(older, None), next(newer, None)
        while old is not None or new is not None:
            if new is None or old is not None and key(old) < key(new):
                yield old, None
                old = next(older, None)
            elif old is None or key(new) < key(old):
                yield None, new
                new = next(newer, None)
            else:
                yield old, new
                old, new = next(older, None), next(newer, None)
