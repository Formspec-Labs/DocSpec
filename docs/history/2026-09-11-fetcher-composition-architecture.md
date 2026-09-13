# Compose only the document transports an experiment needs

The existing `RoutingContentFetcher` will accept any nonempty selection of its
local, HTTPS, and S3 delegates. Callers construct supported fetchers directly
and inject the router through the existing runtime. This removes the need for
dummy local/S3 clients in an HTTPS-only experiment without adding a factory,
configuration language, or plugin registry.

The solutions architect agreed to retain existing route selection and transport
bounds. The router's configuration digest must reflect the actual delegate
settings. Returned child metadata must agree with that delegate and the requested
acquisition before the router relabels it. Refusal closes the stream before any
bytes are accepted. Client creation, credentials, and client lifetime stay with
the caller. Shared pure identity/metadata checks belong beside the existing
fetcher interface; runtime-specific preparation errors remain in the runtime.

Prepared workers must also reject changed settings when they reuse sealed work or
have zero tasks. Recomputing the existing worker description supplies that check;
there is no additional persistent setting identity. Existing transport unit
fixtures and public lifecycle tests will qualify routing, bounds, mutation,
recovery, and refusal without claiming live network verification.

## Ordinary catalog candidates can fetch public S3 objects

The source-catalog rendition vocabulary describes candidate locations, media,
expected hashes, and sizes. It does not carry the adapter's preobserved S3 tuple.
The existing S3 adapter required that tuple, so routing alone could not make a
public catalog experiment use S3.

Keep the generic catalog vocabulary and extend the existing adapter. A candidate
without either an S3 observation or a transport-version pin receives a HEAD
observation, followed by the existing conditional GET and exact response checks.
Canonical locator and configured source boundaries are checked before HEAD;
known or observed size limits refuse before download. A preobserved candidate
must still provide its complete matching metadata and pin. Malformed or partial
input never falls back to a fresh observation. The adapter implementation ID
advances to `docspec.content-fetcher.anonymous-s3.v2`; its live configuration
digest continues to describe the actual configured bounds.

A catalog schema extension would be appropriate if a current provider needed to
require a catalog-time S3 observation. No such production adapter caller exists
in this repository: CourtListener tools use listing-derived versions with HTTPS
locators, and the FR/Mirrulations qualification reuses byte-pinned local captures.
Reinterpreting those histories is outside this change. A single unconditioned
GET could avoid HEAD, but would omit the existing observe-then-require-unchanged
sequence. One additional metadata request per new unpinned acquisition preserves
that behavior without another configuration choice or persistence format.

The capture retains the observed version identity, not raw HEAD headers. The
ordinary byte verifier checks any expected digest independently; the retained
capture becomes the exact input for later processing without another HEAD or GET.
An unpinned location can change between separate acquisition attempts. That is
an acquisition-time observation, not evidence of an earlier catalog-time version.

Qualification uses a supplied-record catalog, the real S3 adapter/router, and an
injected bounded client fixture. It covers capture, retention, later processing,
strict pin refusals, size/hash checks, HEAD-to-GET changes, and stream closure.
It does not establish live public bucket availability. Parent-owned runtime gates
and independent static review record the final validation outcome separately.
