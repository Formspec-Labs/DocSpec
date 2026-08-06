"""Small credential guards for plans, receipts, logs, and CLI diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from docspec.errors import IntegrityError

REDACTED_SECRET = "[redacted: secret-like content]"

_SECRET_PATTERNS = (
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "aws-secret-access-key",
        re.compile(r"\baws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+=]{40}\b", re.IGNORECASE),
    ),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}\b", re.IGNORECASE)),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    ),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("credential-url", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)),
    (
        "credential-assignment",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password|client[_-]?secret)\s*[:=]\s*[^\s,;]{8,}",
            re.IGNORECASE,
        ),
    ),
)
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "accesstoken",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "secret",
        "secretkey",
        "token",
    }
)


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def secret_markers(text: str) -> tuple[str, ...]:
    """Return stable rule names for credentials found in one text value."""

    if not isinstance(text, str):
        raise TypeError("secret scanning requires text")
    return tuple(name for name, pattern in _SECRET_PATTERNS if pattern.search(text))


def redact_text(text: str) -> str:
    """Replace a complete diagnostic value when any credential rule matches."""

    return REDACTED_SECRET if secret_markers(text) else text


def secret_paths(value: Any, *, path: str = "$") -> tuple[str, ...]:
    """Return paths containing secret-like keys or values in a JSON-compatible value."""

    matches: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{path}.{key_text}"
            if _normalized_key(key_text) in _SENSITIVE_KEYS:
                matches.add(child)
            if secret_markers(key_text):
                matches.add(child)
            matches.update(secret_paths(item, path=child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for index, item in enumerate(value):
            matches.update(secret_paths(item, path=f"{path}[{index}]"))
    elif isinstance(value, str) and secret_markers(value):
        matches.add(path)
    return tuple(sorted(matches))


def require_secret_free(value: Any, *, label: str) -> None:
    """Reject identity-bearing material that would persist a credential."""

    matches = secret_paths(value)
    if matches:
        raise IntegrityError(f"{label} contains secret-like content at {list(matches)}")


def redact(value: Any) -> Any:
    """Return a JSON-compatible copy with complete secret-bearing values replaced."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[redact_text(key_text)] = (
                REDACTED_SECRET
                if _normalized_key(key_text) in _SENSITIVE_KEYS
                else redact(item)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
