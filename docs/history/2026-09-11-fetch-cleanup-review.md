# Preserve an interruption when stream cleanup also fails

**Static review: APPROVE.** The independent reviewer inspected the bounded
`FetchStream.__exit__` change and its three new cases. The parent ran the tests.
This fixes exception handling inside a document fetch; execution control belongs
to Dagster and no cancellation API or stop-event layer was introduced.

## Behavior and evidence

| Path | Before | After |
| --- | --- | --- |
| `FetchStream.__exit__` during `KeyboardInterrupt` or source failure | A failing close callback could replace the original exception. | The same original exception continues, with the cleanup error recorded in an exception note. |
| `StoreExecutionService._capture_candidate` | A close `OSError` could turn an interruption into an ordinary acquisition failure. | The original interruption passes through the ordinary `Exception` handler. |
| `FetchStream.close` | Closes at most once, including failed close. | Unchanged; cleanup is not attempted again. |
| Context exits normally, but close fails | Raises the close error. | Unchanged; failure is not hidden. |

The tests assert exact exception object identity for interruption and integrity
failure, the added cleanup evidence, idempotent close, and propagation when no
primary exception exists. The fetcher test file passed **29 tests in 3.04 seconds**
with `uv run --frozen --extra dagster pytest -q tests/test_content_fetchers.py`.
The independent review found no material issue. These checks do not qualify
native Dagster cancellation; that integration remains D20/D21.
