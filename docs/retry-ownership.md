# Retry work at the layer that understands the failure

Dagster owns task retries and recovery. DocSpec records what happened to each
selected document and processor invocation. A transport SDK can retry an
individual HTTP operation when explicitly configured. Those counts describe
different work and should remain distinct.

| Owner | Retried unit | Configuration and evidence |
| --- | --- | --- |
| Dagster | One failed mapped store task | Native `dagster.RetryPolicy` and run events. Verified checkpoints preserve completed document stages when work resumes. |
| DocSpec acquisition | One candidate-file acquisition | The plan's `RetryPolicy` / `WorkLimits.max_attempts`; failures carry the candidate attempt number and successful captures retain acquisition identities. |
| DocSpec processing | One processor invocation on one segment | The same bounded item retry policy; each failed/successful call has a processor-attempt receipt. Permanent failures stop promptly. |
| S3 SDK | One HEAD or GET operation | Native `total_max_attempts`, supplied through `sdk_total_attempts`. SDK requests are not additional DocSpec attempt receipts. |
| Source provider | Its collection operations | The provider owns collection retries and reported observations. DocSpec preserves those reports separately from dataset work. |

The built-in S3 constructor defaults `sdk_total_attempts` to **1**, including the
first request. Increase it only when you intend additional SDK requests inside
each DocSpec acquisition attempt. An unpinned candidate can issue both HEAD and
GET; the SDK limit applies separately to each operation. For example, two
DocSpec attempts with three SDK attempts each can issue six failing HEAD
requests while producing two acquisition failures for one selected document.
This limit follows the SDK's [documented total-attempt option](https://docs.aws.amazon.com/boto3/latest/guide/retries.html#available-configuration-options).

S3 fetcher configuration 2.0 replaces the ambiguous `sdk_max_attempts` field with
`sdk_total_attempts`. The old implementation used the SDK option that counts
retries after the initial request, allowing one extra request. Current producers
use the corrected field and identity; there is no compatibility alias.
`from_boto3` configures the actual native client. Callers who inject another
client remain responsible for that client's internal retry behavior.

A retained accepted document failure is a completed dataset outcome. It does
not make its Dagster task fail. Replaying the saved terminal task reuses that
outcome; use [selected repair](repairing-failures.md) to deliberately revisit
failed documents. A crashed task can instead require native Dagster re-execution.
SDK retries and task retries are configured on their native objects; DocSpec
does not translate them into another scheduler policy.

`WorkLimits` describes logical retained work, not total network transfer,
external billing, or attempts across every native re-execution. Failed transfers
and third-party calls can consume resources without producing a retained blob
or processor result. A process interrupted before saving a checkpoint may
repeat unfinished work. Native run controls and provider limits remain relevant;
the removed runner network-allowance setting did not enforce a global budget.

Failure rows can include unsuccessful attempts followed by eventual success.
Use the document's terminal disposition to count failed documents. Similarly,
provider rejected records, selected documents, scheduled tasks, SDK requests,
processor attempts, and exported text rows are different units. Catalog preview
and inspection retain these distinctions rather than producing one failure total.

The [native SDK tests](../tests/test_s3_native_retries.py) count real HTTP requests
against a local fixture at total-attempt settings 1 and 3 and combine them with
DocSpec attempts. Existing [work-budget tests](../tests/test_work_budget.py) check
attributed processor retries and logical charging, and
[Dagster tests](../tests/test_dagster_adapter.py) check native mapped-task retries.
