"""Build a sealed, portable fingerprint set for predecessor Python code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import keyword
import struct
import subprocess
import token
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "docspec/predecessor-code-fingerprints/v1"
NORMALIZATION_VERSION = "python-syntax-token-v1"
WINDOW_SIZE_TOKENS = 96

_EXCLUDED_TOKEN_TYPES = frozenset(
    {
        token.ENCODING,
        token.COMMENT,
        token.NL,
        token.NEWLINE,
        token.INDENT,
        token.DEDENT,
        token.ENDMARKER,
    }
)


def canonical_json_bytes(value: object) -> bytes:
    """Return the stable JSON representation used to seal the artifact."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_digest(value: bytes) -> str:
    """Return a labeled SHA-256 digest."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def normalized_python_tokens(source: bytes, *, filename: str = "<source>") -> tuple[str, ...]:
    """Normalize valid Python into syntax-bearing tokens.

    Comments, layout-only tokens, identifier spelling, and literal values do
    not affect the result. Python keywords and operators remain exact, so a
    fingerprint describes implementation shape rather than prose or naming.
    """

    ast.parse(source, filename=filename, feature_version=(3, 12))
    normalized: list[str] = []
    for item in tokenize.tokenize(io.BytesIO(source).readline):
        if item.type in _EXCLUDED_TOKEN_TYPES:
            continue
        if item.type == token.NAME:
            if keyword.iskeyword(item.string) or keyword.issoftkeyword(item.string):
                normalized.append(f"NAME:keyword:{item.string}")
            else:
                normalized.append("NAME:identifier")
        elif item.type == token.NUMBER:
            normalized.append("NUMBER:literal")
        elif item.type == token.STRING:
            normalized.append("STRING:literal")
        else:
            normalized.append(f"{token.tok_name[item.type]}:{item.string}")
    return tuple(normalized)


def syntax_window_digest(window: Sequence[str]) -> str:
    """Hash one normalized window with unambiguous length-prefix framing."""

    digest = hashlib.sha256()
    for item in window:
        encoded = item.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def syntax_window_fingerprints(
    normalized_tokens: Sequence[str],
    *,
    window_size: int = WINDOW_SIZE_TOKENS,
) -> Iterable[str]:
    """Yield fingerprints for every complete normalized token window."""

    if window_size <= 0:
        raise ValueError("window_size must be positive")
    for start in range(len(normalized_tokens) - window_size + 1):
        yield syntax_window_digest(normalized_tokens[start : start + window_size])


def artifact_payload_digest(artifact: Mapping[str, Any]) -> str:
    """Seal all artifact fields except the seal itself."""

    payload = {key: value for key, value in artifact.items() if key != "payloadSha256"}
    return sha256_digest(canonical_json_bytes(payload))


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _production_paths(repository: Path, commit: str) -> tuple[str, ...]:
    names = _git(repository, "ls-tree", "-r", "-z", "--name-only", commit).split(b"\0")
    paths = []
    for encoded in names:
        if not encoded:
            continue
        path = encoded.decode("utf-8")
        parsed = PurePosixPath(path)
        if parsed.parts and parsed.parts[0] == "src" and parsed.suffix == ".py":
            paths.append(path)
    return tuple(sorted(paths))


def build_artifact(repository: Path, reference: str) -> dict[str, Any]:
    """Fingerprint every production Python file at one exact Git commit."""

    commit = _git(repository, "rev-parse", "--verify", f"{reference}^{{commit}}").decode().strip()
    symbolic_reference = _git(repository, "rev-parse", "--symbolic-full-name", reference).decode().strip()
    tree = _git(repository, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    committed_at = _git(repository, "show", "-s", "--format=%cI", commit).decode().strip()
    remote_url = _git(repository, "remote", "get-url", "origin").decode().strip()

    files: list[dict[str, Any]] = []
    all_window_fingerprints: set[str] = set()
    source_bytes = 0
    syntax_tokens = 0
    window_occurrences = 0
    for path in _production_paths(repository, commit):
        source = _git(repository, "show", f"{commit}:{path}")
        tokens = normalized_python_tokens(source, filename=f"{commit}:{path}")
        windows = tuple(syntax_window_fingerprints(tokens))
        unique_windows = sorted(set(windows))
        files.append(
            {
                "path": path,
                "byteCount": len(source),
                "blobSha256": sha256_digest(source),
                "syntaxTokenCount": len(tokens),
                "syntaxWindowOccurrenceCount": len(windows),
                "syntaxWindowFingerprints": unique_windows,
            }
        )
        source_bytes += len(source)
        syntax_tokens += len(tokens)
        window_occurrences += len(windows)
        all_window_fingerprints.update(unique_windows)

    artifact: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "baseline": {
            "repositoryUrl": remote_url,
            "reference": reference,
            "symbolicReference": symbolic_reference,
            "commitSha": commit,
            "treeSha": tree,
            "committedAt": committed_at,
            "productionPathRule": "tracked src/**/*.py files",
        },
        "algorithm": {
            "normalizationVersion": NORMALIZATION_VERSION,
            "pythonGrammar": "3.12",
            "parser": "stdlib ast.parse",
            "tokenizer": "stdlib tokenize.tokenize",
            "excludedTokenTypes": sorted(token.tok_name[item] for item in _EXCLUDED_TOKEN_TYPES),
            "identifierNormalization": "preserve keywords and soft keywords; replace other names",
            "literalNormalization": "replace NUMBER and STRING values",
            "windowSizeTokens": WINDOW_SIZE_TOKENS,
            "windowFraming": "four-byte unsigned big-endian length followed by UTF-8 bytes for each token",
            "hashAlgorithm": "sha256",
        },
        "aggregate": {
            "fileCount": len(files),
            "sourceByteCount": source_bytes,
            "syntaxTokenCount": syntax_tokens,
            "syntaxWindowOccurrenceCount": window_occurrences,
            "uniqueSyntaxWindowFingerprintCount": len(all_window_fingerprints),
        },
        "files": files,
        "generator": "tools/predecessor_code_fingerprints.py",
    }
    artifact["payloadSha256"] = artifact_payload_digest(artifact)
    return artifact


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--reference", default="origin/main")
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(arguments)

    artifact = build_artifact(parsed.repository.resolve(), parsed.reference)
    parsed.output.parent.mkdir(parents=True, exist_ok=True)
    parsed.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
