# D11 independent code review

No outstanding correctness finding in the reviewed implementation. One stale
direct-fetcher test was found and corrected by the author during review. A small
typing clarification remains optional: declare the two required identity
properties on the existing `ContentFetcher` protocol.

Scope: the uncommitted D11 changes based on `c99874a`, reviewed independently of
the D16 code I implemented. Static analysis only; I ran no tests. The parent
reported shared passing gates separately. File paths below are relative to
`/Users/mikewolfd/Work/DocSpec`.

## Patch summary

The patch lets callers configure any nonempty subset of local, HTTPS, and S3
fetchers. It binds routing to the configured delegate settings, checks a child's
reported acquisition identity before relabeling it, and rejects changed worker
settings before executing or reusing saved tasks. Unpinned candidates can retain
an observed transport version; an explicit pin still must match.

The main changes are `RoutingContentFetcher` construction, configuration digest,
and `fetch` (`adapters/content_fetchers/routing.py:15`, `:30`, `:41`, `:55`),
shared identity/metadata checks (`ports/content_fetcher.py:35`, `:102`), and the
prepared-worker preflight (`runtime/execution.py:72`). The existing application
capture caller consumes the checked stream at `application/execution.py:456`
and records its metadata at `:469`. The existing runtime composition installs
the bound fetcher before storage constructors (`runtime/composition.py:222`).

## Exploration notes

| Hypothesis | Evidence checked | Result |
| --- | --- | --- |
| H1: optional routes preserve routing and byte limits. | `routing.py:30–75`; routing tests at `tests/test_content_fetchers.py:575`, `:624`, `:643`. | Confirmed: at least one route is required, unknown routes refuse, allowance is the minimum of task and router bounds. |
| H2: changing a delegate setting changes its effective identity. | Local digest at `local_file.py:31`, HTTPS at `https.py:137`, S3 at `s3.py:159`; router digest at `routing.py:41`. | Confirmed for the adapters' declared settings. Externally supplied client internals remain caller-owned. |
| H3: saved work cannot bypass current configuration checks. | `runtime/execution.py:72–105`, `runtime/preparation.py:131–169`, `tests/test_runtime_fetchers.py:60`. | Confirmed for task generation/execution, including sealed work and zero tasks. |
| H4: allowing an unpinned observation does not weaken an explicit transport pin. | `ports/content_fetcher.py:44`, `execution_checkpoints.py:95`, `domain/delivery.py:675`, `tests/test_release_integrity.py:89`. | Confirmed. SQL `IS NOT` additionally catches a missing observed value when the candidate is pinned. |

## Function trace

| Function or method | File:line | Input | Output | Verified behavior |
| --- | --- | --- | --- | --- |
| `LocalFileContentFetcher.configuration_digest` | `adapters/content_fetchers/local_file.py:31` | Current root, chunk size, implementation ID | SHA-256 identity | Reads the current fields used for local acquisition; no cached digest remains. |
| `HttpsContentFetcher.configuration_digest` | `adapters/content_fetchers/https.py:137` | Current immutable config value | Config digest | Uses the config's existing identity serialization at `https.py:72`. |
| `AnonymousS3ContentFetcher.configuration_digest` | `adapters/content_fetchers/s3.py:159` | Current immutable config value | Config digest | Uses the config's existing identity serialization at `s3.py:58`. |
| `RoutingContentFetcher.__post_init__` | `adapters/content_fetchers/routing.py:30` | Optional delegates, object bound | Valid router or `ValueError` | Refuses no delegates, invalid bounds, or missing/invalid identity on any configured route. |
| `RoutingContentFetcher.configuration_digest` | `adapters/content_fetchers/routing.py:41` | All configured delegates and router bound | Deterministic digest | Uses fixed route order, includes only configured routes, and reads their current identity. |
| `RoutingContentFetcher.fetch` | `adapters/content_fetchers/routing.py:55` | Candidate, byte bound, task/attempt IDs | Relabeled `FetchStream` | Selects one configured delegate, caps allowance, checks child metadata and concurrent configuration change before exposing chunks, closes on refusal. |
| `content_fetcher_identity` | `ports/content_fetcher.py:102` | Configured fetcher | Two primitive identity fields | Validates nonblank implementation and SHA-256 config; retains no client details or credentials. |
| `FetchMetadata.verify_request` | `ports/content_fetcher.py:35` | Candidate, selected identity, invocation IDs | None or `IntegrityError` | Checks implementation, configuration, task, attempt, and any explicit transport pin. |
| `_content_fetcher_identity` | `runtime/fetcher.py:10` | Configured fetcher | Identity or `ProfileError` | Reuses the shared validator while preserving preparation's error category. |
| `_BoundContentFetcher.fetch` | `runtime/fetcher.py:26` | Candidate and current invocation | Checked stream | Checks current identity against the preparation snapshot, delegates, and checks returned metadata before bytes are read; closes refused streams. |
| `PreparedLocalRun._require_handoff` | `runtime/execution.py:72` | Supplied handoff | None or refusal | Compares the handoff and current existing worker-description identity, then verifies stage/processor configuration. |
| `PreparedLocalRun.task_source` | `runtime/execution.py:81` | Saved handoff | Task iterator | Runs preflight before opening the planned ledger, even if the iterator will be empty. |
| `PreparedLocalRun.execute_task` | `runtime/execution.py:87` | Handoff and planned task | Store result | Runs preflight before deadline/membership/store checks and before the sealed-delivery branch. |
| `_load_prepared_local_run` | `runtime/preparation.py:131` | Reconstructed services, saved reference | Prepared worker | Existing recovery compares the saved worker bytes and semantic identity with current effective settings. |
| `EntryCheckpointVerifier.verify_entry` | `application/execution_checkpoints.py:69` | Saved entry and plan | Verified checkpoint | Keeps candidate/source/media/digest/size/blob checks; compares observed transport only when a candidate pin exists. |
| `_verify_release_relationships` | `domain/delivery.py:651` | Indexed retained source/file rows | None or refusal | Its transport comparison now treats NULL observed evidence as a mismatch for a non-NULL candidate pin. |

## Data flow and invariants

1. **Effective configuration.** Built-in config properties derive current
   identities. The router hashes those identities plus its byte allowance.
   `_BoundContentFetcher` captures the chosen identity at construction
   (`runtime/fetcher.py:20`); the existing worker description independently
   describes the actual injected object (`runtime/composition.py:83–104`).
   The runtime compares that description before tasks, and the bound fetcher
   compares its snapshot before acquisition. Changing a field after preparation
   must not silently execute under the earlier saved worker identity.
2. **Child evidence before relabeling.** The router captures the child's identity,
   calls it, validates metadata, rechecks route configuration, and only then
   replaces downloader/configuration with router identity
   (`routing.py:61–75`). On refusal, no chunk iterator has been consumed. The
   `FetchStream` close path closes an unstarted source callback as well as its
   iterator (`ports/content_fetcher.py:56–76`).
3. **Transport observations.** A candidate without `transport_version` makes no
   version-equality demand. Local fetch produces `local-stat` when unpinned
   (`local_file.py:62`). That observation is retained by the ordinary capture
   path (`application/execution.py:469–482`). Explicit pins are checked in
   metadata, saved checkpoints, and retained relationships. The SQL change
   prevents three-valued NULL comparison from skipping a missing observation.
4. **Existing transport limits remain in the owning fetcher.** Router allowance
   flows into local size/stream bounds (`local_file.py:57`, `:83`), HTTPS header
   and streamed bounds (`https.py:247–296`), and S3 sealed size plus streamed
   bounds (`s3.py:250–252`, `:304–325`). HTTPS host/redirect validation and S3
   conditional requests/response metadata checks remain unchanged.

## Test behavior and concrete edges

These are source-traced expected outcomes, not tests executed by this reviewer.

| Tests | Source | Expected behavior and supporting trace |
| --- | --- | --- |
| Existing routing, optional HTTPS, unknown route and object limit | `tests/test_content_fetchers.py:575` | Exact local/HTTPS bytes and router identity; missing HTTPS refuses without S3 access; 10-byte limit refuses the larger S3 candidate before I/O. |
| Each route alone, config mutation, empty router | `tests/test_content_fetchers.py:624` | Each permitted singleton constructs; local chunk size or HTTPS/S3 config replacement changes router digest; no routes raises `ValueError`. |
| Wrong child implementation/config/task/attempt/version and mutation during fetch | `tests/test_content_fetchers.py:643` | All six cases refuse before the failing chunk generator executes; close callback count is one. |
| Real local unpinned candidate, run/retain/inspect/recover | `tests/test_runtime_fetchers.py:45` | Retains observed `local-stat`, router downloader identity, one captured file; repeated run returns the saved run. |
| Mutation after completed or zero-task run | `tests/test_runtime_fetchers.py:60` | Same object and reconstructed handoff refuse after local route mutation. Preflight occurs before the empty task ledger can bypass it. |
| Direct fetcher mutation | `tests/test_local_worker_identity.py:162` | Corrected during review: execute_task and task_source both raise preflight `IntegrityError`; fetch count stays zero. |
| Missing/different required observed version in release | `tests/test_release_integrity.py:89` | Both changes recreate valid file identity but violate source-candidate relationship, exercising `IS NOT` rather than unrelated identity checks. |
| Existing runtime metadata mismatch closure | `tests/test_local_worker_identity.py:126` | Five metadata mismatches close the source, consume no chunks, and create no captured file. |
| Existing provider lifecycle tests | `tests/test_content_fetchers.py:196`, `:240`, `:272`, `:300`, `:311`, `:344`, `:380`, `:409`, `:437`, `:452`, `:490`, `:513`, `:561` | Source assertions cover unstarted closure, local containment, real httpx API via MockTransport, exact host/redirect rules, size/truncation failures, S3 conditional request and response pins, and normalized errors. The patch leaves these branches intact. |

No live network qualification was performed. Client creation/credentials/client
lifetime remain caller responsibilities; a configuration digest describes the
adapter's declared settings, not an audit of arbitrary injected client internals
(`https.py:131–135`, `s3.py:153–157`, `routing.py:21`). This is the intended
existing scope, not a new request for an identity framework.

## Findings

**F1 — resolved during review.** Severity: WARNING; category: test coverage.
The earlier `test_mutated_fetcher_cannot_run_under_prepared_identity` expected a
terminal store result after changing fetcher settings. The new preflight raises
before task execution, so that assertion was stale. The author changed it to
assert refusal for both direct execution and task generation, plus zero fetches
(`tests/test_local_worker_identity.py:162–171`). No production change was needed.

**F2 — optional typing clarification.** Severity: OBSERVATION; category:
maintainability. `ContentFetcher` declares only `fetch()`
(`ports/content_fetcher.py:91–99`), while both router and public runtime require
`downloader_id` and `configuration_digest` (`ports/content_fetcher.py:102–109`).
Declaring those two read-only properties on the existing protocol would make
the current requirement visible to implementors and type checkers. Keep the
runtime validation; no new identity API is needed.

## Conclusion

VERDICT: APPROVE

- The patch supplies optional route composition through one existing fetcher
  path and preserves checked metadata, saved settings, and byte limits.
- Coverage of changed paths: ADEQUATE for this bounded local/provider-fixture
  change. Live service qualification is outside this review.
- Confidence: HIGH for the code paths and assertions traced above. Execution
  results are owned by the parent's shared test gates.

## Parent execution evidence

The focused transport gate passed **84 tests in 2.84 seconds** using
`uv run --frozen --extra dagster pytest -q` over actual local/S3 public runtime
fixtures, transport adapters/router, worker identity, and retained release
integrity. HTTPS coverage exercises the adapter/router with mocked responses;
local and S3 fixtures exercise the ordinary catalog lifecycle. Installed custom
fetcher injection remains covered by the package probe.

The fixed combined tree then passed **1,184 tests in 154.41 seconds**, with one
live integration test deselected and one known example-import warning. This
full run includes the catalog and failed-item-repair commits plus the final
transport changes. It establishes local regression/package behavior; it does
not establish live network availability, CI, push, merge, or deployment.
