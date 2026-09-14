# Choose how an experiment fetches document bytes

Pass a configured fetcher explicitly to `CoreWorkspace.documents(fetcher=...)`.
The selected source state determines documents and candidate files; the fetcher
supplies bounded bytes and acquisition facts. [Python runs](python-runs.md)
shows document import and execution.

The [GovInfo bill example](govinfo-bill-example.md) injects an installed SpicyDocs
fetcher for one explicitly selected XML version, then processes retained bytes
again after closing the source client.

`RoutingContentFetcher` accepts any nonempty combination of `local`, `https`, and
`s3`. Unconfigured schemes refuse. It applies the smaller of the caller's
byte allowance and an optional `max_object_bytes` allowance.

```python
from docspec.adapters.content_fetchers import LocalFileContentFetcher, RoutingContentFetcher

fetcher = RoutingContentFetcher(
    local=LocalFileContentFetcher(input_directory),
    max_object_bytes=8 * 1024**2,
)
```

For HTTPS, configure exact allowed hosts and a user agent through
`HttpsContentFetcherConfig`, then supply `HttpsContentFetcher` as `https`.
`HttpsContentFetcher.from_httpx(config)` can create its optional client.
The built-in S3 adapter is `AnonymousS3ContentFetcher`, with explicit bucket and
prefix bounds; it supports unsigned public objects. Ordinary catalog candidates
can name canonical `s3://bucket/key` locators, including through
[supplied records](catalog-inputs.md). The adapter observes an unpinned object's
size, ETag, and modification time with HEAD, checks size limits, and downloads
with `IfMatch` set to that ETag. It rejects a response whose size, ETag, or
modification time differs, and verifies streamed length. Expected SHA-256 and
size values supplied in the catalog still apply independently.

```python
from docspec.adapters.content_fetchers import AnonymousS3ContentFetcher, AnonymousS3ContentFetcherConfig

fetcher = RoutingContentFetcher(s3=AnonymousS3ContentFetcher(
    unsigned_s3_client,
    AnonymousS3ContentFetcherConfig(bucket="public-example", prefix="documents"),
))
```

The extra HEAD request occurs only for new acquisition with neither a required
transport version nor an S3 observation. A candidate with preobserved S3 metadata
must supply its complete matching transport pin; partial or malformed pins refuse
without a new observation. DocSpec retains the observed transport-version identity
and captured bytes, not the raw HEAD response. Later processing from those retained
captures makes no S3 requests. This records the version fetched at acquisition
time; an unpinned catalog does not promise the object remains at its earlier version.

Install the `http` or `s3` extra when using the corresponding client factory.
Importing these adapters does not import those optional clients. Callers own
client lifetime and close clients they create. The router does not close shared
clients after an individual fetch; it closes each returned stream when needed.
Authenticated sources can use caller-supplied fetchers, keeping credentials
outside retained configuration.

`AnonymousS3ContentFetcher.from_boto3` defaults to one native SDK request per
HEAD or GET operation. Its `sdk_total_attempts` setting includes the first call;
additional SDK retries are an explicit choice. Injected clients own their actual
retry behavior. See [retry ownership](retry-ownership.md) for how SDK requests,
document attempts, and Dagster task retries are counted separately.

## What a custom fetcher must report

A configured fetcher declares a nonempty `downloader_id` and a SHA-256
`configuration_digest`. The digest identifies behavior-affecting configuration;
it must change when those settings change. `fetch` returns a `FetchStream` with
metadata identifying the same implementation, settings, task, and attempt.
The runtime checks these facts before reading bytes. A router checks its child's
metadata before recording its own configured routing identity.

An optional candidate transport version is a required pin when supplied. If the
catalog provides no version, the fetcher may record an observed one. The actual
local-file adapter records its filesystem observation; DocSpec preserves it in
the capture. A missing or different observed version refuses when the candidate
explicitly required one. Expected digest and byte-size checks remain independent.

The bound fetcher validates its implementation and configuration before reading
bytes. Those settings contribute to the Core operation definition; changing
them changes the requested operation. Existing results are selected only when
the common correspondence and policy checks permit reuse. An explicit
continuation must still match its pinned definition.
