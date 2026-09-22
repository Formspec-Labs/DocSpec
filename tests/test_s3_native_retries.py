"""Retry accounting against a real SDK client: a local HTTP server answers HEAD/GET so the S3 fetcher's
sdk_total_attempts limit is proven to include the initial request for each operation, and SDK transport retries
stay separate from attributed document attempts in the runtime.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from docspec.adapters.content_fetchers import AnonymousS3ContentFetcher, AnonymousS3ContentFetcherConfig, S3ContentFetcherError
from docspec.domain import core
from docspec.domain.content import CandidateFile, SourceItem
from docspec.runtime.core import CoreWorkspace


@contextmanager
def _sdk_fetcher(monkeypatch, *, total_attempts, failing_operation):
    """Yield a fetcher whose SDK client is redirected to a local server, leaving native retry configuration intact."""
    boto3 = pytest.importorskip("boto3")
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            calls.append(self.command)
            self.send_response(500 if self.command == failing_operation else 200)
            self.send_header("Content-Length", "0")
            self.send_header("ETag", '"source-version"')
            self.send_header("Last-Modified", "Fri, 11 Sep 2026 12:00:00 GMT")
            self.end_headers()

        do_HEAD = respond
        do_GET = respond

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = boto3.client
    try:
        with monkeypatch.context() as patch:
            # Only redirect the public constructor's actual SDK client to a
            # local fixture. Native SDK retry configuration remains untouched.
            patch.setattr(boto3, "client", lambda *args, **kwargs: client(
                *args, endpoint_url=f"http://127.0.0.1:{server.server_port}", **kwargs,
            ))
            fetcher = AnonymousS3ContentFetcher.from_boto3(AnonymousS3ContentFetcherConfig(
                bucket="public-example", prefix="documents", sdk_total_attempts=total_attempts,
            ))
        try:
            yield fetcher, calls
        finally:
            fetcher.client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("total_attempts", [1, 3])
@pytest.mark.parametrize("operation", ["HEAD", "GET"])
def test_native_sdk_limit_includes_initial_request_for_each_operation(monkeypatch, total_attempts, operation):
    candidate = CandidateFile("body", "s3://public-example/documents/input.txt", "text/plain")
    with _sdk_fetcher(monkeypatch, total_attempts=total_attempts, failing_operation=operation) as (fetcher, calls):
        with pytest.raises(S3ContentFetcherError):
            fetcher.fetch(candidate, max_bytes=1024, task_id="task", attempt_id="attempt")
        assert calls.count(operation) == total_attempts
        if operation == "GET":
            assert calls.count("HEAD") == 1



@pytest.mark.parametrize("document_attempts", [1, 2])
def test_sdk_retries_remain_separate_from_attributed_document_attempts(monkeypatch, tmp_path, document_attempts):
    with _sdk_fetcher(monkeypatch, total_attempts=3, failing_operation="HEAD") as (fetcher, calls):
        with CoreWorkspace(tmp_path / "dataset") as workspace:
            pipeline = workspace.documents(fetcher=fetcher)
            pipeline.import_sources([SourceItem("document", "1", (CandidateFile("body",
                "s3://public-example/documents/input.txt", "text/plain"),))], state_id="source")
            for attempt in range(document_attempts):
                with pytest.raises(S3ContentFetcherError):
                    pipeline.run("source", run_id=f"attempt-{attempt}", extract=False, segment=False)
            with workspace.ledger._transaction() as connection:
                keys = connection.execute("SELECT kind,record_id FROM records WHERE kind='result'").fetchall()
            records = [record.value for batch in workspace.ledger.read_records(keys) for record in batch]
            results = [record for record in records if isinstance(record, core.Result) and record.outcome.status == "failed"]
            assert len(results) == document_attempts
            assert all(result.outcome.status == "failed" for result in results)
            assert len({result.execution_id for result in results}) == document_attempts
            assert calls == ["HEAD"] * (3 * document_attempts)
