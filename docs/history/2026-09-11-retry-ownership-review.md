# D23: retry ownership review

## 1. Summary and scope

**Static verdict: APPROVE.** The S3 adapter now expresses total SDK attempts correctly and defaults to one. Dagster keeps task retries, the SDK keeps HTTP-operation retries, and DocSpec keeps its existing bounded, attributable document/processor attempts. This removes ambiguity without adding a retry service or another scheduler policy.

Reviewed `adapters/content_fetchers/s3.py`, `tests/test_s3_native_retries.py`, the retry ownership guide/architecture note, and the corrected `docs/python-runs.md` limit description. Traced the existing acquisition, processor, runtime-policy, work-budget and native Dagster retry paths. Concurrent canonical JSON and result-export changes are excluded. Review was static; no reviewer tests/builds/source changes/commits.

## 2. Function and data traces

- `AnonymousS3ContentFetcherConfig` at `s3.py:26–75` now has positive integer `sdk_total_attempts=1`, config format 2.0, and the identity-bearing `sdkTotalAttempts` field. No legacy alias remains.
- `from_boto3` at `s3.py:164–191` passes that value to native `Config(retries={"total_max_attempts": ..., "mode": "standard"})`; SDK imports remain inside the optional constructor. The count applies separately to each HEAD or GET operation.
- `application/execution.py:432–496` retains the existing per-candidate loop. It assigns acquisition task/attempt identities, classifies each failed DocSpec attempt, stops nonretryable failures, and charges successful captured bytes once. It does not invent receipts for hidden SDK requests.
- `application/processor_runtime.py:142–263` retains the existing exact-input cache and bounded invocation loop. Every actual completed failed/successful processor call produces an attempt receipt; logical invocation charging is distinct from individual external calls.
- `runtime/composition.py:108–116` checks the retry policy digest and exact agreement between its attempt ceiling and `plan.limits.max_attempts` before composing work.
- Native `dagster.RetryPolicy` remains injected at the adapter boundary. Existing tests at `tests/test_dagster_adapter.py:179` and `:285` cover mapped-task retry and native configuration injection. An accepted document failure is a completed task outcome, not a request to retry the whole task.

The reviewer independently checked the [AWS Botocore Config documentation](https://docs.aws.amazon.com/botocore/latest/reference/config.html): `total_max_attempts` includes the initial request; the `Config.max_attempts` option counts retries after it. The selected change directly uses the native total-attempt option rather than translating it through a second policy model.

## 3. Invariants and simplification judgment

The retained loops have different observable meanings. Replacing document and processor attempts with Dagster step retries would lose item-attributed failure outcomes and processor receipts, and would blur an accepted failed document with an interrupted task. Keeping those existing loops is justified. Adding a shared retry service would add abstraction without replacing this required distinction.

SDK retries are disabled by default through total=1, preventing automatic multiplication inside each document attempt. Explicit SDK retries remain available. A document can issue HEAD and GET, each with its own native ceiling; this is not a document-wide HTTP request cap.

The configuration qualifies `from_boto3`, which builds the actual client. Callers supplying a client manually remain responsible for that client's internal retries. WorkLimits continues to describe logical work, not all failed transfer bytes, SDK requests, external billing or aggregate work across native re-executions. The stale runner-network-allowance claim is removed rather than replaced by unsupported enforcement.

## 4. Test evidence

`tests/test_s3_native_retries.py` starts a local HTTP server and redirects only the public constructor's SDK endpoint. Native SDK retry configuration and request execution remain real.

- The HEAD/GET × total=1/3 cases count actual HTTP requests and assert the SDK includes the initial request in the configured total. For GET failure, the successful HEAD occurs once separately.
- The public supplied-catalog/runtime integration uses SDK total=3 and DocSpec attempts=1/2. It asserts three/six HTTP HEAD requests, one scheduled document, zero captured files, one/two numbered transient failure rows, terminal `accepted-failure`, and no extra requests when the saved terminal task is run again.
- Existing `tests/test_work_budget.py:278` covers logical captured-byte charging after transport retry; `:441` and `:473` cover processor retry success/exhaustion and receipts. Existing native Dagster tests retain their separate scheduler ownership.

**Parent execution:** first focused gate 61 passed in 11.09 seconds. The final extended gate completed with **102 passed in 17.34 seconds** at `/tmp/docspec-retry-ownership-final-gate.log`; the reviewer read that completion line. Counts overlap and are not presented as an aggregate or a new whole-worktree full regression. The parent installed the frozen S3 and Dagster extras for this qualification; the new real-SDK tests otherwise use `importorskip("boto3")`.

## 5. Findings and remaining limits

Resolved: the old `sdk_max_attempts=3` used the native option that permits three retries after the initial request, obscuring the true total. The new field, native option, format identity and actual-request tests agree.

Resolved: the user guide claimed a runner network allowance after that ineffective setting had been removed. The guide now separates concurrency/index limits from logical document work and network/billing behavior.

No material source finding remains. Qualification does not include external production S3 behavior, manually injected clients' hidden retries, universal physical-cost limits or a cross-run attempt ledger. Such claims would require evidence beyond this bounded change and are explicitly excluded.

## 6. Verdict and D23 acceptance

**APPROVE.** The code and inspected tests support explicit retry ownership, correct bounded SDK totals, attributable DocSpec attempts, and unchanged terminal/checkpoint reuse. The checklist's former wording about one effective run policy should be aligned with the native-owner decision: explicit SDK, task and item policies, with accurately distinguished accounting. This review supports the bounded D23 outcome and does not establish a global network or billing budget.
