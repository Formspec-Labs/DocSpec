"""Anonymous public-S3 fetcher contract: an unpinned candidate observes its revision with HEAD and streams the
body back under IfMatch, while a preobserved candidate uses its exact pin without a HEAD and is never silently
re-observed.

Covers locator and source-boundary checks before any request, known-size and max_bytes refusals before download,
invalid observation metadata and post-HEAD drift refusals that close the body, classified provider errors keeping
their meaning, and a catalog capture retaining the observed transport version so later processing never refetches.
"""

from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO

import pytest

from docspec.adapters.content_fetchers import (
    AnonymousS3ContentFetcher, AnonymousS3ContentFetcherConfig, RoutingContentFetcher,
    S3ContentFetcherError, s3_locator, s3_transport_version,
)
from docspec.domain import core
from docspec.domain.content import CandidateFile, SourceItem
from docspec.runtime.core import CoreWorkspace
from docspec.domain.identity import sha256_digest
from docspec.errors import IntegrityError, LimitExceededError


BUCKET, KEY = "public-example", "documents/notes.txt"
CONTENT = b"Contribution notes."
MODIFIED = datetime(2026, 9, 11, 12, tzinfo=UTC)
OBSERVATION = {
    "bucket": BUCKET, "key": KEY, "size": len(CONTENT), "etag": '"revision-1"',
    "lastModified": "2026-09-11T12:00:00Z",
}
VERSION = s3_transport_version(
    bucket=BUCKET, key=KEY, size=len(CONTENT), etag=OBSERVATION["etag"], last_modified=MODIFIED,
)


class _ProviderError(Exception):
    def __init__(self, code, status):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
        super().__init__("provider-only details")


class _Client:
    """Stub S3 client with injectable HEAD/GET errors, post-HEAD response changes and recorded request bodies."""

    def __init__(self):
        self.headers = {"ContentLength": len(CONTENT), "ETag": OBSERVATION["etag"], "LastModified": MODIFIED}
        self.get_changes = {}
        self.head_error = self.get_error = None
        self.requests, self.bodies = [], []

    def head_object(self, **request):
        self.requests.append(("HEAD", request))
        if self.head_error is not None:
            raise self.head_error
        return dict(self.headers)

    def get_object(self, **request):
        self.requests.append(("GET", request))
        if self.get_error is not None:
            raise self.get_error
        body = BytesIO(CONTENT)
        self.bodies.append(body)
        return self.headers | self.get_changes | {"Body": body}


def _fetcher(client):
    return AnonymousS3ContentFetcher(client, AnonymousS3ContentFetcherConfig(
        bucket=BUCKET, prefix="documents", chunk_size=4,
    ))


def _candidate(**changes):
    return CandidateFile("body", s3_locator(BUCKET, KEY), "text/plain", **changes)


def _fetch(client, candidate=None, *, max_bytes=4096):
    return _fetcher(client).fetch(
        _candidate() if candidate is None else candidate,
        max_bytes=max_bytes, task_id="task", attempt_id="attempt",
    )


def test_unpinned_s3_observes_then_conditionally_streams_with_the_observed_version():
    client = _Client()
    with _fetch(client) as stream:
        assert stream.metadata.transport_version == VERSION
        assert b"".join(stream.chunks) == CONTENT
    assert client.requests == [
        ("HEAD", {"Bucket": BUCKET, "Key": KEY}),
        ("GET", {"Bucket": BUCKET, "Key": KEY, "IfMatch": OBSERVATION["etag"]}),
    ]
    assert client.bodies[0].closed


def test_preobserved_s3_uses_its_exact_pin_without_head():
    client = _Client()
    candidate = _candidate(expected_size=len(CONTENT), transport_version=VERSION, metadata={"s3": OBSERVATION})
    with _fetch(client, candidate) as stream:
        assert stream.metadata.transport_version == VERSION
        assert b"".join(stream.chunks) == CONTENT
    assert [method for method, _ in client.requests] == ["GET"]
    assert client.bodies[0].closed


@pytest.mark.parametrize("changes", [
    {"transport_version": VERSION},
    {"metadata": {"s3": None}},
    {"metadata": {"s3": {"etag": OBSERVATION["etag"]}}},
    {"metadata": {"s3": OBSERVATION}, "expected_size": len(CONTENT)},
    {"metadata": {"s3": OBSERVATION}, "transport_version": VERSION},
    {"metadata": {"s3": OBSERVATION}, "transport_version": "wrong", "expected_size": len(CONTENT)},
])
def test_partial_or_malformed_preobserved_s3_never_falls_back_to_head(changes):
    client = _Client()
    with pytest.raises(IntegrityError):
        _fetch(client, _candidate(**changes))
    assert client.requests == []


@pytest.mark.parametrize("locator", [
    "s3://other/documents/notes.txt", "s3://public-example/outside/notes.txt",
    "s3://public-example/documents/notes.txt?version=1", "s3://public-example/documents/%6eotes.txt",
])
def test_unpinned_s3_checks_locator_and_source_boundary_before_head(locator):
    client = _Client()
    with pytest.raises(IntegrityError):
        _fetch(client, replace(_candidate(), locator=locator))
    assert client.requests == []


@pytest.mark.parametrize(("expected_size", "max_bytes", "expected_error", "requests"), [
    (len(CONTENT), 2, LimitExceededError, []),
    (None, 2, LimitExceededError, ["HEAD"]),
    (len(CONTENT) + 1, 4096, IntegrityError, ["HEAD"]),
])
def test_unpinned_s3_refuses_known_or_observed_size_before_download(expected_size, max_bytes, expected_error, requests):
    client = _Client()
    with pytest.raises(expected_error):
        _fetch(client, _candidate(expected_size=expected_size), max_bytes=max_bytes)
    assert [method for method, _ in client.requests] == requests


@pytest.mark.parametrize(("field", "value"), [
    ("ContentLength", True), ("ContentLength", -1), ("ETag", None), ("LastModified", None),
])
def test_invalid_s3_head_metadata_refuses_before_get(field, value):
    client = _Client()
    client.headers[field] = value
    with pytest.raises(IntegrityError, match="observation metadata"):
        _fetch(client)
    assert [method for method, _ in client.requests] == ["HEAD"]


@pytest.mark.parametrize(("operation", "code", "status", "expected_error"), [
    ("head", "NoSuchKey", 404, IntegrityError),
    ("head", "SlowDown", 503, S3ContentFetcherError),
    ("get", "PreconditionFailed", 412, IntegrityError),
])
def test_s3_observation_and_conditional_request_errors_keep_their_meaning(operation, code, status, expected_error):
    client = _Client()
    setattr(client, f"{operation}_error", _ProviderError(code, status))
    with pytest.raises(expected_error) as error:
        _fetch(client)
    assert "provider-only" not in str(error.value)
    assert client.bodies == []


@pytest.mark.parametrize(("field", "value"), [
    ("ETag", '"changed"'), ("ContentLength", len(CONTENT) + 1),
    ("LastModified", datetime(2026, 9, 12, tzinfo=UTC)),
])
def test_s3_changes_after_head_refuse_before_body_read_and_close(field, value):
    client = _Client()
    client.get_changes[field] = value
    with pytest.raises(IntegrityError, match="differs from the sealed candidate"):
        _fetch(client)
    assert client.bodies[0].closed



def _source(expected_digest=None):
    return SourceItem("notes", "revision1", (CandidateFile("body", s3_locator(BUCKET, KEY), "text/plain",
        expected_digest=sha256_digest(CONTENT) if expected_digest is None else expected_digest,
        expected_size=len(CONTENT)),))


def test_public_s3_catalog_capture_retains_observation_and_later_processing_does_not_fetch(tmp_path):
    client = _Client()
    fetcher = RoutingContentFetcher(s3=_fetcher(client))
    with CoreWorkspace(tmp_path / "dataset") as workspace:
        pipeline = workspace.documents(fetcher=fetcher)
        pipeline.import_sources([_source()], state_id="source")
        pipeline.run("source", run_id="captured", extract=False, segment=False)
        records = [record.value for batch in workspace.ledger.retained_records() for record in batch]
        capture = next(record.value.value for record in records if isinstance(record, core.Entity)
            and isinstance(record.value, core.InlineValue) and isinstance(record.value.value, dict)
            and "transportVersion" in record.value.value)
        assert capture["transportVersion"] == VERSION
        assert capture["blob"]["digest"] == sha256_digest(CONTENT)
        assert capture["downloaderId"] == fetcher.downloader_id
    with CoreWorkspace(tmp_path / "dataset") as workspace:
        pipeline = workspace.documents(fetcher=fetcher)
        pipeline.run("source", run_id="processed")
        summary = list(pipeline.rows("processed"))[0][2]
        assert len(summary["selections"]) == 3
    assert [method for method, _ in client.requests] == ["HEAD", "GET"]
    assert all(body.closed for body in client.bodies)


def test_public_s3_expected_content_digest_is_checked_independently_of_transport(tmp_path):
    client = _Client()
    with CoreWorkspace(tmp_path / "dataset") as workspace:
        pipeline = workspace.documents(fetcher=RoutingContentFetcher(s3=_fetcher(client)))
        pipeline.import_sources([_source(sha256_digest(b"other bytes"))], state_id="source")
        with pytest.raises(IntegrityError, match="expected digest"):
            pipeline.run("source", run_id="failed", extract=False, segment=False)
        with workspace.ledger._transaction() as connection:
            keys = connection.execute("SELECT kind,record_id FROM records WHERE kind='result'").fetchall()
        records = [record.value for batch in workspace.ledger.read_records(keys) for record in batch]
        results = [record for record in records if isinstance(record, core.Result) and record.outcome.status == "failed"]
        assert len(results) == 1 and results[0].outcome.status == "failed"
        assert workspace.ledger.current("documents") is None
    assert [method for method, _ in client.requests] == ["HEAD", "GET"]
    assert all(body.closed for body in client.bodies)
