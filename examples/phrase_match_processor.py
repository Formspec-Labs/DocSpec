"""An injected local phrase matcher, kept in examples rather than the library.

Matches use literal phrases, Unicode word boundaries, and original UTF-8 byte
offsets. All overlapping phrases are retained; no semantic classification occurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import monotonic

from docspec.domain.identity import canonical_json_bytes, parse_closed_json, sha256_digest, thaw_json
from docspec.domain.core_admission import record_value
from docspec.domain import core
from docspec.domain.processor_policy import ProcessorLimits, ProcessorResponse

from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.artifacts import utf8_byte_offsets

MAX_RESOURCE_BYTES = 64 * 1024
MAX_TERMS, MAX_PHRASES, MAX_PHRASE_CHARACTERS, MAX_MATCHES = 64, 256, 128, 1024


def _terms(resource_bytes: bytes) -> tuple[tuple[str, str, str], ...]:
    """Parse the closed vocabulary and enforce its term, phrase and character bounds."""
    value = thaw_json(parse_closed_json(resource_bytes, label="phrase vocabulary"))
    if not isinstance(value, dict) or set(value) != {"terms"} or not isinstance(value["terms"], list):
        raise ValueError("phrase vocabulary requires only a terms array")
    if not 1 <= len(value["terms"]) <= MAX_TERMS:
        raise LimitExceededError("phrase vocabulary exceeds its term-count bounds")
    identifiers, result = set(), []
    for term in value["terms"]:
        if not isinstance(term, dict) or set(term) != {"id", "label", "phrases"}:
            raise ValueError("vocabulary term requires id, label, and phrases")
        if any(not isinstance(term[key], str) or not term[key].strip() for key in ("id", "label")):
            raise ValueError("vocabulary term identity and label must be nonempty text")
        if term["id"] in identifiers:
            raise ValueError("vocabulary term identity is duplicated")
        identifiers.add(term["id"])
        phrases = term["phrases"]
        if not isinstance(phrases, list) or not phrases:
            raise ValueError("vocabulary term requires nonempty phrases")
        seen = set()
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > MAX_PHRASE_CHARACTERS:
                raise ValueError("vocabulary phrase must be bounded nonempty text")
            if phrase in seen:
                raise ValueError("vocabulary phrase is duplicated within a term")
            seen.add(phrase)
            result.append((term["id"], term["label"], phrase))
            if len(result) > MAX_PHRASES:
                raise LimitExceededError("phrase vocabulary exceeds its phrase-count bound")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True, init=False)
class PhraseMatcher:
    """Literal phrase algorithm; Core/provider adapter owns invocation checks."""

    resource: core.Resource
    limits: ProcessorLimits
    patterns: tuple

    def __init__(self, resource: core.Resource, resource_bytes: bytes, *, case_sensitive=False, limits=ProcessorLimits()):
        """Reject a non-bytes, oversized or digest-mismatched vocabulary and a non-bool flag, then compile patterns."""
        if not isinstance(resource_bytes, bytes):
            raise TypeError("phrase vocabulary must be immutable bytes")
        if len(resource_bytes) > MAX_RESOURCE_BYTES:
            raise LimitExceededError("phrase vocabulary exceeds its byte bound")
        if sha256_digest(resource_bytes) != resource.description["digest"]:
            raise IntegrityError("phrase vocabulary bytes differ from the resource pin")
        if type(case_sensitive) is not bool:
            raise ValueError("case_sensitive must be a boolean")
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "patterns", tuple((identifier, label, phrase, re.compile(re.escape(phrase), 0 if case_sensitive else re.IGNORECASE))
            for identifier, label, phrase in _terms(resource_bytes)))

    def __call__(self, payload):
        """Match every phrase and return the bounded, ordered response, refusing past the duration or output bound."""
        started = monotonic()
        content, evidence = payload["content"], payload["evidence"]
        text = content.decode("utf-8")
        offsets = utf8_byte_offsets(text)
        matches, match_bytes = [], 0
        for identifier, label, phrase, pattern in self.patterns:
            position = 0
            while found := pattern.search(text, position):
                start, end = found.span()
                position = start + 1
                if monotonic() - started > self.limits.max_duration_seconds:
                    raise LimitExceededError("phrase matcher exceeds its duration bound")
                if (start and (text[start - 1].isalnum() or text[start - 1] == "_")) or (
                    end < len(text) and (text[end].isalnum() or text[end] == "_")
                ):
                    continue
                match = {"termId": identifier, "label": label, "phrase": phrase, "quote": text[start:end],
                         "segmentByteStart": offsets[start], "segmentByteEnd": offsets[end]}
                match_bytes += len(canonical_json_bytes(match))
                if len(matches) >= MAX_MATCHES or match_bytes > self.limits.max_output_bytes:
                    raise LimitExceededError("phrase matches exceed their output bound")
                matches.append(match)
        matches.sort(key=lambda match: (match["segmentByteStart"], match["segmentByteEnd"], match["termId"], match["phrase"]))
        value = {"segmentDigest": sha256_digest(content), "resource": self.resource.description,
                 "enclosingSourceEvidence": evidence, "matches": matches}
        return ProcessorResponse((value,), "application/json", resources=(record_value(self.resource, core.Resource),))
