"""Bounded JSON input and secret-aware output shared by command groups."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from docspec.domain.identity import canonical_json_file_bytes, parse_canonical_json, parse_closed_json, thaw_json
from docspec.domain.security import redact, require_secret_free
from docspec.errors import DocSpecError

MAX_JSON_BYTES = 16 * 1024 * 1024


class CliError(DocSpecError):
    """The requested operator action failed preflight or verification."""


class SourceCatalogCliError(DocSpecError):
    """A source-catalog operator action failed preflight or verification."""


def read_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int = MAX_JSON_BYTES,
    error_type: type[DocSpecError] = CliError,
) -> bytes:
    """Read one regular non-symlink file, bounding the actual read rather than just the stat.

    Every refusal raises error_type.
    """

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise error_type(f"{label} must be a regular, non-symlink file: {path}")
    if path.stat().st_size > max_bytes:
        raise error_type(f"{label} exceeds the {max_bytes}-byte limit")
    # Stat is an inexpensive preflight, not the resource bound: a file can grow
    # after it. Read at most one byte beyond the limit and check the actual read.
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise error_type(f"{label} exceeds the {max_bytes}-byte limit")
    return payload


def read_object(
    path: Path,
    *,
    label: str,
    canonical: bool = False,
    error_type: type[DocSpecError] = CliError,
) -> dict[str, Any]:
    """Read one bounded JSON object, parsed canonically when requested.

    A non-object raises error_type; malformed or non-canonical bytes fail with IntegrityError from the shared parser.
    """

    payload = read_bytes(path, label=label, error_type=error_type)
    parser = parse_canonical_json if canonical else parse_closed_json
    value = thaw_json(parser(payload, label=label))
    if not isinstance(value, dict):
        raise error_type(f"{label} must be a JSON object")
    return value


def existing_root(path: Path, *, label: str, error_type: type[DocSpecError] = CliError) -> Path:
    """Return the resolved path of an existing non-symlink directory, or raise error_type."""

    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise error_type(f"{label} must be an existing, non-symlink directory: {path}")
    return path.resolve(strict=True)


def emit(value: object, *, error: bool = False) -> None:
    """Write canonical JSON to stdout, or redacted JSON to stderr when error is set.

    Successful output must be secret-free; error output is redacted instead of refused.
    """

    if error:
        value = redact(value)
    else:
        require_secret_free(value, label="CLI output")
    stream = sys.stderr.buffer if error else sys.stdout.buffer
    stream.write(canonical_json_file_bytes(value))
    stream.flush()


def emit_error(error: Exception) -> int:
    """Emit the versioned CLI error envelope to stderr and return exit code 2."""

    emit({"format": "docspec-cli-error", "formatVersion": "1.0",
          "errorType": type(error).__name__, "message": str(error), "verdict": "fail"}, error=True)
    return 2
