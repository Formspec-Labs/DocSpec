# D11 public S3 acquisition — independent static review

VERDICT: APPROVE. No material finding remains in the S3 acquisition addition or the inspected shared version-check adjustments.

Repository: `/Users/mikewolfd/Work/DocSpec`; reviewed the option-B S3 changes in `src/docspec/adapters/content_fetchers/s3.py`, `tests/test_runtime_s3_fetcher.py`, `docs/fetchers.md`, and `docs/history/2026-09-11-fetcher-composition-architecture.md`. Also traced the shared fetch-metadata check, checkpoint/release required-version checks, public catalog conversion, and final negative-test adjustments. The parent assigned the broader router extraction to a separate independent reviewer; this report supplies the additional S3/public-lifecycle assessment and does not replace that review.

This follows `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. No tests/builds were executed by this reviewer, no repository code was edited, and unrelated history was not read. Parent-executed focused/full outcomes are separate evidence.

## 1. Patch summary

An ordinary catalog candidate can now name a public S3 object without fabricating the adapter's preobserved metadata. When neither an S3 observation nor a required transport-version pin is supplied, the existing adapter observes size, ETag and modification time with HEAD, then performs its existing conditional GET and response checks. It records the observed version identity with the captured bytes.

Candidates with any preobserved S3 metadata or required transport version remain on the strict path: incomplete, malformed, inconsistent or mismatched observations refuse without a fresh HEAD. The catalog schema is unchanged. The adapter advances to `docspec.content-fetcher.anonymous-s3.v2`, and its configuration digest reflects its actual current bounds.

This resolves a concrete integration gap: the generic catalog candidate vocabulary could express an S3 locator and expected content digest/size, but could not supply the old adapter's required S3 tuple. The solution reuses the existing conditional acquisition path instead of adding a second downloader or a new catalog metadata model.

## 2. Function trace

| Function / method | File:line | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `_candidate_location` | `src/docspec/adapters/content_fetchers/s3.py:189` | Candidate locator → bucket/key | Canonical URI spelling, no credentials/query/fragment/port, exact configured bucket and key prefix checked before network calls. |
| `_observed_record` | `src/docspec/adapters/content_fetchers/s3.py:212` | Bucket/key → checked observation | HEAD only; provider failure classified without exposing provider details in public error text; validates size/ETag/time and closes an unexpected injected body. |
| `_candidate_record` | `src/docspec/adapters/content_fetchers/s3.py:234` | Candidate and optional sealed observation → observation | Fresh HEAD requires both absent version and absent `s3` metadata. Otherwise requires the full matching observation, expected size and derived transport pin. |
| `_s3_version_content`, `s3_transport_version` | `src/docspec/adapters/content_fetchers/s3.py:86`, `:107` | Bucket/key/size/ETag/time → normalized facts and stable version ID | Reuses the existing identity recipe; timezone-aware datetimes become UTC and size must be a nonnegative integer. |
| `fetch` | `src/docspec/adapters/content_fetchers/s3.py:270` | Candidate and task allowance → `FetchStream` | Known size refuses before HEAD; observed size refuses before GET; conditional request uses observed ETag; exact response metadata validated before body iteration. |
| Nested `chunks`, `close_body_once` | `src/docspec/adapters/content_fetchers/s3.py:342`, `:336` | Streaming response → bounded bytes | Existing non-byte, length, overflow and truncation refusal; explicit close is idempotent and closes before consumption or on early/error exit. |
| `FetchMetadata.verify_request` | `src/docspec/ports/content_fetcher.py:34` | Returned acquisition facts and requested candidate → validation | Exact implementation/config/task/attempt; a supplied candidate transport version requires equal observed version. Absence of a required pin permits recording an observation. |
| `_BoundContentFetcher.fetch` | `src/docspec/runtime/fetcher.py:26` | Prepared implementation and candidate → verified stream | Rechecks configured identity, applies the shared metadata rule, closes on refusal before accepting bytes. |
| `EntryCheckpointVerifier.verify_entry` | `src/docspec/application/execution_checkpoints.py:69` | Saved capture and requested candidate → checked evidence | Required version, expected digest and expected size remain checked; a previously unpinned candidate may have a retained observed version. |
| Logical release relationships | `src/docspec/domain/delivery.py:651` | Captured files and source candidates → admission | A required version cannot be absent or different; SQL uses null-safe inequality for the observed field. |
| Public runtime fixture | `tests/test_runtime_s3_fetcher.py:178` | Supplied catalog S3 candidate → ordinary experiment | Uses existing public catalog types without raw S3 metadata, real adapter/router and standard capture/retain/recovery/processing. |

`from_boto3` still imports optional SDK modules only when invoked and configures unsigned requests. The caller owns the injected client's lifetime. The stream owns its returned response body; the router does not close a shared client after one fetch.

## 3. Data flow and invariants

1. **Network observation has a defined trigger.** The new path is for transport-unpinned candidates, which may still have expected byte size or SHA-256. Neither a partial S3 observation nor a lone required version is silently replaced by fresh metadata (`s3.py:234`).
2. **Location and size limits precede download.** Canonical locator and configured source bounds are checked before HEAD. A known oversize candidate refuses without network calls; observed oversize or expected-size disagreement refuses after HEAD but before GET (`s3.py:282`, `:286`).
3. **Observation is bound to the returned object.** HEAD supplies `IfMatch`; GET must also return the same ETag, size, and modification time before body iteration. The stream verifies actual byte count. This does not treat ETag as a content SHA-256: ordinary capture independently verifies a supplied expected digest (`tests/test_runtime_s3_fetcher.py:233`).
4. **The capture records what was acquired.** Returned metadata carries the derived observed transport ID, acquisition time, implementation/configuration, task and attempt. The caller's catalog remains unchanged. Raw HEAD headers are not retained, and the docs make no catalog-time version guarantee for an unpinned location.
5. **Retained inputs remain the later processing authority.** Replaying sealed work, reconstructing the same handoff, planning an unchanged base successor, and processing retained capture do not repeat HEAD or GET. The public lifecycle test compares the full retained file payload, not only its byte digest (`tests/test_runtime_s3_fetcher.py:203`).
6. **Required pins remain strict.** The shared version change distinguishes no requirement from a required value; it does not permit a mismatch when a value was requested. Router and logical release negative cases still enforce explicit pins (`tests/test_content_fetchers.py:642`; `tests/test_release_integrity.py:89`).
7. **Acquisition-time choice is explicit.** Separate fresh acquisition attempts against an unpinned location may observe different versions. The implementation does not pretend the earlier catalog froze a mutable remote object. A retained capture supplies exact later input; source coverage, publisher completeness and live bucket availability remain unproven.

The added complexity is justified by a current ordinary catalog use case and preserves an already implemented observe/conditional-download pattern. A catalog schema extension is unnecessary for this acquisition-time path. No additional persistence format or configuration switch is required.

## 4. Test behavior and edge cases

These are statically inspected assertions, not reviewer-executed results.

| Test | File:line | Evidence inspected |
| --- | --- | --- |
| Unpinned observation | `tests/test_runtime_s3_fetcher.py:81` | Exact HEAD then GET request sequence, `IfMatch` value, observed version identity, returned bytes and body closure. |
| Existing complete pin | `tests/test_runtime_s3_fetcher.py:93` | Complete preobserved candidate performs GET only and preserves its exact version. |
| Partial/malformed pins | `tests/test_runtime_s3_fetcher.py:111` | Lone version, null/partial observation, missing version/size and wrong version all refuse with no network calls. |
| Locator/source boundary | `tests/test_runtime_s3_fetcher.py:122` | Wrong bucket/prefix, query string and noncanonical URI spelling refuse before HEAD. |
| Known/observed byte bounds | `tests/test_runtime_s3_fetcher.py:134` | Known oversize makes no call; observed oversize and expected-size disagreement make only HEAD. |
| Invalid observation | `tests/test_runtime_s3_fetcher.py:144` | Boolean/negative size and missing ETag/time refuse before GET. |
| Provider/refusal classification | `tests/test_runtime_s3_fetcher.py:157` | Missing object, temporary provider problem and conditional failure retain intended failure classes; public messages omit provider-only details. |
| Changed object response | `tests/test_runtime_s3_fetcher.py:170` | Changed ETag, size or modification time refuses and closes returned body before the fetch method can return a byte iterator. |
| Public lifecycle | `tests/test_runtime_s3_fetcher.py:203` | Supplied catalog → capture/retain → sealed replay → saved-handoff recovery → zero-task successor → later processing. Exact file payload persists, representations/segments appear, reuse counts are correct, and total calls remain one HEAD and one GET. |
| Independent expected digest | `tests/test_runtime_s3_fetcher.py:233` | Wrong expected SHA-256 yields zero captures and a failure, prevents retaining the rejected run, and closes the body. |
| Required child version | `tests/test_content_fetchers.py:642` | Router receives an explicit required version; wrong returned metadata refuses with no chunk reads and one close. |
| Required retained version | `tests/test_release_integrity.py:89` | Missing or different observed version refuses when the source candidate required one. |

The tests use an injected bounded S3 client fixture and real DocSpec services. They do not contact a live bucket or qualify the optional SDK against real deployment conditions. Existing stream-overflow/truncation tests remain the owner of unchanged GET body behavior.

## 5. Findings

No material finding in the final S3 slice. The initial ordinary-catalog integration gap is closed by the real public lifecycle test rather than only a manually constructed low-level `CandidateFile` test.

The parent removed `transport_version` from a generic runtime mismatch fixture whose catalog supplied no required version. That adjustment is correct: the same shared checker remains covered by the explicit-pin router negative and retained release negatives. Implementation/configuration/task/attempt mismatches still refuse before byte reads. The prepared-worker mutation test also now verifies earlier handoff refusal and zero calls, rather than requiring a later rejected store as an implementation detail.

`docs/fetchers.md` accurately describes the extra HEAD, strict preobserved path, captured observed ID rather than raw headers, independent expected hash/size, optional client ownership, and no network calls for later retained processing. No live acquisition or remote immutability claim is made.

## 6. Conclusion and acceptance

**APPROVE, high confidence for the scoped static review.** Ordinary public-catalog S3 acquisition now works through the existing runtime without a new catalog schema, while preobserved candidates preserve strict pins. Bounds and closure precede accepted body data, observed identity reaches retained capture, and later work reuses that exact capture.

This supplies the previously missing S3 ordinary-lifecycle evidence for D11. The parent should combine it with the separate router review and executed frozen-snapshot gates before marking the complete D11 item accepted. No tests/builds were executed by this reviewer; no final runtime pass count is asserted here.

Additional D11 acceptance judgment: another mocked HTTPS public-lifecycle test is not required by the stated acceptance. The generic router/runtime seam is covered by local and S3 public lifecycles and installed custom injection. HTTPS separately has real `httpx.MockTransport` streaming coverage and an actual HTTPS-adapter/router fixture; unlike the previous S3 path, it has no additional catalog metadata prerequisite. Qualification should preserve those distinctions instead of claiming every transport was exercised end to end or over a live network.

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
