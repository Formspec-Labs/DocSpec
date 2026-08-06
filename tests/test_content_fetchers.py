from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.content_fetchers import (
    AnonymousS3ContentFetcher,
    AnonymousS3ContentFetcherConfig,
    RoutingContentFetcher,
    S3ContentFetcherError,
    public_s3_url,
    s3_locator,
    s3_transport_version,
)
from docspec.adapters.source_catalog import LocalFileContentFetcher
from docspec.domain.content import CandidateFile
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream

LAST_MODIFIED = "2026-08-06T12:00:00Z"


class _S3Error(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"provider error {code}")
        self.response = {
            "Error": {"Code": code, "Message": "provider-only detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _Body:
    def __init__(self, payload: bytes, *, fail_after_reads: int | None = None) -> None:
        self.payload = payload
        self.position = 0
        self.fail_after_reads = fail_after_reads
        self.read_count = 0
        self.read_sizes: list[int] = []
        self.close_count = 0

    @property
    def closed(self) -> bool:
        return self.close_count > 0

    def read(self, amount: int) -> bytes:
        if self.closed:
            raise RuntimeError("body is closed")
        self.read_count += 1
        self.read_sizes.append(amount)
        if self.fail_after_reads is not None and self.read_count > self.fail_after_reads:
            raise OSError("provider stream failed")
        end = min(self.position + amount, len(self.payload))
        result = self.payload[self.position : end]
        self.position = end
        return result

    def close(self) -> None:
        self.close_count += 1


class _Client:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.etag = '"etag-v1"'
        self.last_modified: object = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        self.content_length: object = len(payload)
        self.body_payload = payload
        self.fail_after_reads: int | None = None
        self.error: Exception | None = None
        self.requests: list[dict[str, Any]] = []
        self.bodies: list[_Body] = []

    def get_object(self, **request: Any) -> dict[str, Any]:
        self.requests.append(dict(request))
        if self.error is not None:
            raise self.error
        body = _Body(self.body_payload, fail_after_reads=self.fail_after_reads)
        self.bodies.append(body)
        return {
            "Body": body,
            "ETag": self.etag,
            "LastModified": self.last_modified,
            "ContentLength": self.content_length,
        }


def _config(*, chunk_size: int = 3) -> AnonymousS3ContentFetcherConfig:
    return AnonymousS3ContentFetcherConfig(
        bucket="mirrulations",
        prefix="raw-data/SEC/SEC-202",
        chunk_size=chunk_size,
    )


def _candidate(payload: bytes = b"exact bytes") -> CandidateFile:
    bucket = "mirrulations"
    key = "raw-data/SEC/SEC-202/example/documents/SEC-2020-0001-0001.json"
    return CandidateFile(
        "metadata-json",
        s3_locator(bucket, key),
        "application/json",
        expected_size=len(payload),
        transport_version=s3_transport_version(
            bucket=bucket,
            key=key,
            size=len(payload),
            etag='"etag-v1"',
            last_modified=LAST_MODIFIED,
        ),
        metadata={
            "s3": {
                "bucket": bucket,
                "key": key,
                "size": len(payload),
                "etag": '"etag-v1"',
                "lastModified": LAST_MODIFIED,
            },
            "publicSourceUrl": public_s3_url(bucket=bucket, key=key, region_name="us-east-1"),
        },
    )


def test_fetch_stream_closes_unstarted_source_once() -> None:
    closed: list[str] = []

    def chunks() -> Any:
        yield b"unused"

    stream = FetchStream(
        FetchMetadata(
            "fetcher",
            f"sha256:{'0' * 64}",
            None,
            LAST_MODIFIED,
            "task",
            "attempt",
        ),
        chunks(),
        close_callback=lambda: closed.append("closed"),
    )

    stream.close()
    stream.close()

    assert closed == ["closed"]


def test_local_fetcher_does_not_double_close_descriptors_under_concurrency(tmp_path: Path) -> None:
    source = b"concurrent local bytes"
    (tmp_path / "source.txt").write_bytes(source)
    fetcher = LocalFileContentFetcher(tmp_path, chunk_size=3)
    candidate = CandidateFile(
        "local",
        "source.txt",
        "text/plain",
        expected_size=len(source),
    )

    def read(index: int) -> bytes:
        with fetcher.fetch(candidate, max_bytes=1024, task_id=f"task-{index}", attempt_id="attempt") as stream:
            return b"".join(stream.chunks)

    with ThreadPoolExecutor(max_workers=16) as executor:
        assert list(executor.map(read, range(500))) == [source] * 500


def test_anonymous_s3_fetcher_streams_pinned_object_and_closes() -> None:
    client = _Client(b"exact bytes")
    fetcher = AnonymousS3ContentFetcher(client, _config())
    candidate = _candidate()

    with fetcher.fetch(candidate, max_bytes=20, task_id="task", attempt_id="attempt") as stream:
        assert b"".join(stream.chunks) == b"exact bytes"
        assert stream.metadata.transport_version == candidate.transport_version

    assert client.requests == [
        {
            "Bucket": "mirrulations",
            "Key": "raw-data/SEC/SEC-202/example/documents/SEC-2020-0001-0001.json",
            "IfMatch": '"etag-v1"',
        }
    ]
    assert client.bodies[0].read_sizes == [3, 3, 3, 3, 3]
    assert client.bodies[0].close_count == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("etag", '"changed"', "ETag"),
        ("last_modified", datetime(2026, 8, 7, 12, 0, tzinfo=UTC), "last-modified"),
        ("content_length", 10, "content length"),
    ],
)
def test_anonymous_s3_fetcher_rejects_changed_response_and_closes(
    field: str,
    value: object,
    message: str,
) -> None:
    client = _Client(b"exact bytes")
    setattr(client, field, value)
    fetcher = AnonymousS3ContentFetcher(client, _config())

    with pytest.raises(IntegrityError, match=message):
        fetcher.fetch(_candidate(), max_bytes=20, task_id="task", attempt_id="attempt")

    assert client.bodies[0].close_count == 1


def test_anonymous_s3_fetcher_closes_truncated_oversized_and_failed_streams() -> None:
    truncated = _Client(b"exact bytes")
    truncated.body_payload = b"short"
    with AnonymousS3ContentFetcher(truncated, _config()).fetch(
        _candidate(),
        max_bytes=20,
        task_id="task",
        attempt_id="attempt",
    ) as stream:
        with pytest.raises(IntegrityError, match="truncated"):
            b"".join(stream.chunks)
    assert truncated.bodies[0].close_count == 1

    oversized = _Client(b"exact bytes")
    oversized.body_payload = b"exact bytes plus"
    with AnonymousS3ContentFetcher(oversized, _config()).fetch(
        _candidate(),
        max_bytes=11,
        task_id="task",
        attempt_id="attempt",
    ) as stream:
        with pytest.raises(LimitExceededError, match="11-byte"):
            b"".join(stream.chunks)
    assert oversized.bodies[0].close_count == 1

    failed = _Client(b"exact bytes")
    failed.fail_after_reads = 1
    with AnonymousS3ContentFetcher(failed, _config()).fetch(
        _candidate(),
        max_bytes=20,
        task_id="task",
        attempt_id="attempt",
    ) as stream:
        with pytest.raises(S3ContentFetcherError, match="streaming read"):
            b"".join(stream.chunks)
    assert failed.bodies[0].close_count == 1


def test_anonymous_s3_fetcher_closes_on_consumer_error_and_before_iteration() -> None:
    consumer_error = _Client(b"exact bytes")
    with pytest.raises(RuntimeError, match="consumer"):
        with AnonymousS3ContentFetcher(consumer_error, _config()).fetch(
            _candidate(),
            max_bytes=20,
            task_id="task",
            attempt_id="attempt",
        ):
            raise RuntimeError("consumer failed")
    assert consumer_error.bodies[0].close_count == 1

    unstarted = _Client(b"exact bytes")
    stream = AnonymousS3ContentFetcher(unstarted, _config()).fetch(
        _candidate(),
        max_bytes=20,
        task_id="task",
        attempt_id="attempt",
    )
    stream.close()
    assert unstarted.bodies[0].close_count == 1


def test_anonymous_s3_fetcher_fails_before_io_for_bounds_and_source_escape() -> None:
    client = _Client(b"exact bytes")
    fetcher = AnonymousS3ContentFetcher(client, _config())

    with pytest.raises(LimitExceededError, match="10-byte"):
        fetcher.fetch(_candidate(), max_bytes=10, task_id="task", attempt_id="attempt")
    escaped = replace_candidate(
        _candidate(),
        locator=s3_locator("mirrulations", "raw-data/OTHER/document.json"),
        metadata={
            "s3": {
                "bucket": "mirrulations",
                "key": "raw-data/OTHER/document.json",
                "size": 11,
                "etag": '"etag-v1"',
                "lastModified": LAST_MODIFIED,
            }
        },
    )
    with pytest.raises(IntegrityError, match="source boundary"):
        fetcher.fetch(escaped, max_bytes=20, task_id="task", attempt_id="attempt")

    assert client.requests == []


def replace_candidate(candidate: CandidateFile, **changes: Any) -> CandidateFile:
    values = {
        "candidate_id": candidate.candidate_id,
        "locator": candidate.locator,
        "media_type": candidate.media_type,
        "expected_digest": candidate.expected_digest,
        "expected_size": candidate.expected_size,
        "transport_version": candidate.transport_version,
        "metadata": candidate.metadata,
    }
    values.update(changes)
    if "metadata" in changes:
        raw = changes["metadata"]["s3"]
        values["transport_version"] = s3_transport_version(
            bucket=raw["bucket"],
            key=raw["key"],
            size=raw["size"],
            etag=raw["etag"],
            last_modified=raw["lastModified"],
        )
    return CandidateFile(**values)


def test_anonymous_s3_fetcher_normalizes_provider_failures_without_leaking_detail() -> None:
    client = _Client(b"exact bytes")
    fetcher = AnonymousS3ContentFetcher(client, _config())
    client.error = _S3Error("PreconditionFailed", 412)
    with pytest.raises(IntegrityError, match="ETag changed") as changed:
        fetcher.fetch(_candidate(), max_bytes=20, task_id="task", attempt_id="attempt")
    assert "provider-only" not in str(changed.value)

    client.error = _S3Error("SlowDown", 503)
    with pytest.raises(S3ContentFetcherError, match="acquisition failed") as unavailable:
        fetcher.fetch(_candidate(), max_bytes=20, task_id="task", attempt_id="attempt")
    assert "provider-only" not in str(unavailable.value)


def test_routing_fetcher_pins_delegate_configuration_and_rejects_unknown_scheme(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "document.txt").write_text("local", encoding="utf-8")
    local = LocalFileContentFetcher(source_root)
    client = _Client(b"exact bytes")
    s3 = AnonymousS3ContentFetcher(client, _config())
    routing = RoutingContentFetcher(local=local, s3=s3)

    local_candidate = CandidateFile("local", "document.txt", "text/plain", expected_size=5)
    with routing.fetch(local_candidate, max_bytes=10, task_id="task", attempt_id="attempt") as stream:
        assert b"".join(stream.chunks) == b"local"
        assert stream.metadata.downloader_id == routing.downloader_id
        assert stream.metadata.downloader_configuration_digest == routing.configuration_digest

    with pytest.raises(IntegrityError, match="scheme"):
        routing.fetch(
            CandidateFile("remote", "https://example.test/document", "text/plain"),
            max_bytes=10,
            task_id="task",
            attempt_id="attempt",
        )
    assert client.requests == []

    bounded = RoutingContentFetcher(local=local, s3=s3, max_object_bytes=10)
    with pytest.raises(LimitExceededError, match="10-byte"):
        bounded.fetch(_candidate(), max_bytes=20, task_id="task", attempt_id="attempt")
    assert client.requests == []
