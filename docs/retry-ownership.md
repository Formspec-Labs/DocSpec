# Distinguish SDK retries from actual attempts

Core records actual executions. The scheduler owns whether another execution
should start, and a provider SDK can retry an individual transport operation
inside one execution. Those counts describe different work.

| Owner | Retried work |
| --- | --- |
| Dagster | Native scheduled operation, with run events, retry policy and cancellation |
| Core | Actual execution and authoritative progress; explicit verified continuation retains the original attempt |
| S3 SDK | A HEAD or GET call, bounded by the configured `sdk_total_attempts` |
| Source provider | Its collection calls and reported source observations |

The built-in S3 constructor defaults `sdk_total_attempts` to one, including the
initial request. An unpinned object may require both HEAD and GET; each has its
own SDK limit. Two actual Core attempts with three SDK attempts each can issue
six failed HEAD requests while retaining two failed Core results. An injected
SDK client remains responsible for its own internal retry behavior.

Core does not add a second generic retry loop. A successful completed selection
can be recovered without invoking the producer. Explicit failed-work repair uses
a new request/run as appropriate; [continuation](operations.md) verifies an
actual checkpoint before resuming the same execution.

Failed transfers and interrupted calls may consume resources without producing
retained output. Retained byte counts are not network-transfer or billing totals.
Keep provider limits and native scheduling controls explicit. The
[S3 integration tests](../tests/test_s3_native_retries.py) verify the actual SDK
request counts against recorded Core failures.
