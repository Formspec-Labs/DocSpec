"""Use real SDK requests to distinguish transport retries from document attempts."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from docspec.adapters.content_fetchers import AnonymousS3ContentFetcher, AnonymousS3ContentFetcherConfig, S3ContentFetcherError
from docspec.domain.content import CandidateFile
from docspec.domain.jobs import FailureClass
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.runtime import build_local_catalog, open_local_inspection, prepare_local_experiment
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from tests.support.source_catalog import producer


@contextmanager
def _sdk_fetcher(monkeypatch, *, total_attempts, failing_operation):
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
    workspace = LocalWorkspace(tmp_path / "dataset")
    namespace = "urn:test:sdk-retries"
    source = SuppliedRecordSource(({
        "recordId": "document", "sourceIssuedVersion": "1", "title": "Document", "metadata": {},
        "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/plain", "immutable-object", "s3://public-example/documents/input.txt",
        ).to_dict()],
    },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
        max_records=10, max_bytes=1024**2)
    catalog = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id=namespace, producer=producer(), max_scratch_bytes=8 * 1024**2)
    with _sdk_fetcher(monkeypatch, total_attempts=3, failing_operation="HEAD") as (fetcher, calls):
        with prepare_local_experiment(catalog.reference, workspace,
            limits=WorkLimits(10, 1024**2, 100, 100, 100, 16 * 1024**2, 60, max_attempts=document_attempts),
            retry_policy=RetryPolicy(max_attempts=document_attempts, base_delay_milliseconds=0),
            accepted_failure_policy=AcceptedFailurePolicy(accepted_classes=(FailureClass.TRANSIENT_EXTERNAL,)),
            source_catalog_producer=producer(), document_release_producer=producer(),
            completed_at="2026-09-11T00:00:00Z", deadline_epoch_seconds=4102444800,
            content_fetcher=fetcher, stop_after="capture",
        ) as prepared:
            run = prepared.run()
            result = prepared.retain(run)
            view = open_local_inspection(prepared.plan, workspace, document_release_producer=producer(), release_ref=result)
            assert calls == ["HEAD"] * (3 * document_attempts)
            assert view.summary()["work"]["counts"]["scheduledItems"] == 1
            assert view.summary()["work"]["counts"]["newCapturedFiles"] == 0
            failures = tuple(view.records("failures"))
            assert len(failures) == document_attempts
            assert sorted(row["payload"]["attempt"] for row in failures) == list(range(1, document_attempts + 1))
            assert {row["payload"]["failureClass"] for row in failures} == {"transient-external"}
            assert [row["payload"]["disposition"] for row in view.records("dispositions")] == ["accepted-failure"]
            assert prepared.run() == run
            assert calls == ["HEAD"] * (3 * document_attempts)
