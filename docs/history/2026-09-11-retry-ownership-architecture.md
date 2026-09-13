# Keep native retries and attributable document attempts distinct

The independent architect approved a bounded correction: use the S3 SDK's
native total-attempt option, default it to one, and explain each existing retry
owner. No retry service, global network ledger, or scheduler policy is added.

The SDK's `Config.max_attempts` counts retries after the initial request, while
DocSpec's old `sdk_max_attempts` name suggested a total. Configuration 2.0 uses
`sdk_total_attempts` and maps it directly to native `total_max_attempts`, which
includes the initial request. The [AWS documentation](https://docs.aws.amazon.com/botocore/latest/reference/config.html)
establishes this distinction. Explicit additional SDK retries remain useful
without nesting them by default inside every document attempt.

The alternative was to replace DocSpec's per-candidate and per-processor loops
with Dagster step retries. That would erase their different purpose: bounded,
item-attributed failure outcomes and processor attempt receipts. A store task
can complete while containing an accepted document failure; a native step retry
does not mean that the user chose to repair that failure. Existing bounded loops
and checkpoint semantics therefore remain, while Dagster continues to own task
execution, retries, cancellation and native run events.

The configuration qualifies the built-in SDK constructor. Manually injected
clients own their own hidden retries. Neither configuration nor logical work
accounting establishes a universal request, network-byte, billing, or
cross-re-execution budget. The stale guide sentence claiming a runner network
allowance is removed. No new enforcement claim replaces it.

Qualification uses actual SDK HTTP requests against a local server for HEAD
and GET, including total limits 1 and 3. Integrated acquisition distinguishes
SDK requests from DocSpec attempts, preserves final failure attribution, writes
no capture on failure, and does no further fetching when replaying a saved
terminal task. Existing processor and native Dagster retry tests remain the
owners of their respective behavior. Root execution and independent review are
recorded separately after the final gate.
