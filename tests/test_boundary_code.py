from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from tools.predecessor_code_fingerprints import (
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
    WINDOW_SIZE_TOKENS,
    artifact_payload_digest,
    normalized_python_tokens,
    sha256_digest,
    syntax_window_fingerprints,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = ROOT / "src" / "docspec"
FINGERPRINT_ARTIFACT = ROOT / "conformance" / "predecessor-code-fingerprints-v1.json"
PINNED_PREDECESSOR_COMMIT = "f1fcb8c9c8838071e9c45462799db788971baca4"
PINNED_PREDECESSOR_TREE = "bc87ead259760ac6ae596a67522f4e1240a433a6"
PINNED_PREDECESSOR_REMOTE = "git@github.com:civictechdc/spicy-regs.git"
PINNED_ARTIFACT_PAYLOAD_SHA256 = "sha256:45ad74eff5238da47f0fb32ecd2830eba3a50f11940847fc2bd4de4b457d33d2"
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@cache
def _artifact() -> dict[str, Any]:
    return json.loads(FINGERPRINT_ARTIFACT.read_text(encoding="utf-8"))


def _production_files() -> tuple[Path, ...]:
    return tuple(sorted(PRODUCTION_ROOT.rglob("*.py")))


def test_predecessor_fingerprint_artifact_is_closed_complete_and_sealed() -> None:
    artifact = _artifact()
    assert set(artifact) == {
        "schemaVersion",
        "baseline",
        "algorithm",
        "aggregate",
        "files",
        "generator",
        "payloadSha256",
    }
    assert artifact["schemaVersion"] == SCHEMA_VERSION
    assert artifact["payloadSha256"] == PINNED_ARTIFACT_PAYLOAD_SHA256
    assert artifact["payloadSha256"] == artifact_payload_digest(artifact)

    baseline = artifact["baseline"]
    assert set(baseline) == {
        "repositoryUrl",
        "reference",
        "symbolicReference",
        "commitSha",
        "treeSha",
        "committedAt",
        "productionPathRule",
    }
    assert baseline == {
        "repositoryUrl": PINNED_PREDECESSOR_REMOTE,
        "reference": "origin/main",
        "symbolicReference": "refs/remotes/origin/main",
        "commitSha": PINNED_PREDECESSOR_COMMIT,
        "treeSha": PINNED_PREDECESSOR_TREE,
        "committedAt": "2026-07-31T12:57:02-04:00",
        "productionPathRule": "tracked src/**/*.py files",
    }

    algorithm = artifact["algorithm"]
    assert set(algorithm) == {
        "normalizationVersion",
        "pythonGrammar",
        "parser",
        "tokenizer",
        "excludedTokenTypes",
        "identifierNormalization",
        "literalNormalization",
        "windowSizeTokens",
        "windowFraming",
        "hashAlgorithm",
    }
    assert algorithm["normalizationVersion"] == NORMALIZATION_VERSION
    assert algorithm["windowSizeTokens"] == WINDOW_SIZE_TOKENS == 96
    assert algorithm["hashAlgorithm"] == "sha256"

    files = artifact["files"]
    assert files
    assert [item["path"] for item in files] == sorted(item["path"] for item in files)
    assert len({item["path"] for item in files}) == len(files)
    for item in files:
        assert set(item) == {
            "path",
            "byteCount",
            "blobSha256",
            "syntaxTokenCount",
            "syntaxWindowOccurrenceCount",
            "syntaxWindowFingerprints",
        }
        assert item["path"].startswith("src/") and item["path"].endswith(".py")
        assert item["byteCount"] >= 0
        assert item["syntaxTokenCount"] >= 0
        assert item["syntaxWindowOccurrenceCount"] == max(
            0, item["syntaxTokenCount"] - WINDOW_SIZE_TOKENS + 1
        )
        assert SHA256_PATTERN.fullmatch(item["blobSha256"])
        windows = item["syntaxWindowFingerprints"]
        assert windows == sorted(set(windows))
        assert all(SHA256_PATTERN.fullmatch(value) for value in windows)

    aggregate = artifact["aggregate"]
    assert aggregate == {
        "fileCount": len(files),
        "sourceByteCount": sum(item["byteCount"] for item in files),
        "syntaxTokenCount": sum(item["syntaxTokenCount"] for item in files),
        "syntaxWindowOccurrenceCount": sum(item["syntaxWindowOccurrenceCount"] for item in files),
        "uniqueSyntaxWindowFingerprintCount": len(
            {
                fingerprint
                for item in files
                for fingerprint in item["syntaxWindowFingerprints"]
            }
        ),
    }


def test_active_production_contains_no_predecessor_blob_or_substantial_syntax_window() -> None:
    artifact = _artifact()
    predecessor_blob_paths: dict[str, list[str]] = {}
    predecessor_window_paths: dict[str, list[str]] = {}
    for item in artifact["files"]:
        predecessor_blob_paths.setdefault(item["blobSha256"], []).append(item["path"])
        for fingerprint in item["syntaxWindowFingerprints"]:
            predecessor_window_paths.setdefault(fingerprint, []).append(item["path"])

    exact_matches: list[str] = []
    syntax_matches: list[str] = []
    production_files = _production_files()
    assert production_files
    for path in production_files:
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_bytes()
        blob_match = predecessor_blob_paths.get(sha256_digest(source))
        if source and blob_match:
            exact_matches.append(f"{relative} == {', '.join(blob_match)}")

        tokens = normalized_python_tokens(source, filename=relative)
        for token_start, fingerprint in enumerate(syntax_window_fingerprints(tokens)):
            window_match = predecessor_window_paths.get(fingerprint)
            if window_match:
                syntax_matches.append(
                    f"{relative} token[{token_start}:{token_start + WINDOW_SIZE_TOKENS}] "
                    f"matches {', '.join(window_match)}"
                )
                break

    assert exact_matches == [], "exact predecessor blobs found:\n" + "\n".join(exact_matches)
    assert syntax_matches == [], "normalized predecessor syntax windows found:\n" + "\n".join(syntax_matches)


def test_normalization_detects_reformatted_and_renamed_implementation() -> None:
    original_lines = ["def calculate_total(records):", "    total = 0"]
    renamed_lines = ["def compute_value(items):", "    result = 999"]
    for index in range(24):
        original_lines.append(f"    total = total + records[{index}]  # source step {index}")
        renamed_lines.append(f"    result=result+items[{index + 100}]")
    original_lines.append("    return total")
    renamed_lines.append("    return result")

    original = normalized_python_tokens(("\n".join(original_lines) + "\n").encode())
    renamed = normalized_python_tokens(("\n".join(renamed_lines) + "\n").encode())
    assert len(original) >= WINDOW_SIZE_TOKENS
    assert set(syntax_window_fingerprints(original)) & set(syntax_window_fingerprints(renamed))


def test_short_boilerplate_does_not_create_a_syntax_fingerprint() -> None:
    tokens = normalized_python_tokens(b"from __future__ import annotations\n")
    assert len(tokens) < WINDOW_SIZE_TOKENS
    assert tuple(syntax_window_fingerprints(tokens)) == ()
