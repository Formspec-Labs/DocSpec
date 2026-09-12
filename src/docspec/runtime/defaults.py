"""Shared explicit defaults for the local Python and command-line runners."""

from docspec.domain.execution import ExecutionLimits


def local_execution_limits(
    *,
    worker_count: int = 1,
    max_concurrency_per_worker: int = 1,
    max_in_flight: int | None = None,
    max_scratch_bytes_per_worker: int = 4 * 1024**3,
    max_network_bytes_per_task: int = 8 * 1024**3,
    request_rate_limit_per_second: int = 100,
    max_provider_concurrency: int = 4,
    max_task_attempts: int = 1,
    retry_initial_delay_milliseconds: int = 0,
    retry_max_delay_milliseconds: int = 0,
) -> ExecutionLimits:
    """Return the existing local-run bounds without enlarging requested limits.

    Omitting ``max_in_flight`` uses the worker count, matching the CLI. These
    are allowances, not allocations. A plan exceeding them still refuses;
    callers can supply explicit overrides instead of changing hidden defaults.
    """

    return ExecutionLimits(
        worker_count=worker_count,
        max_concurrency_per_worker=max_concurrency_per_worker,
        max_in_flight=worker_count if max_in_flight is None else max_in_flight,
        max_scratch_bytes_per_worker=max_scratch_bytes_per_worker,
        max_network_bytes_per_task=max_network_bytes_per_task,
        request_rate_limit_per_second=request_rate_limit_per_second,
        max_provider_concurrency=max_provider_concurrency,
        max_task_attempts=max_task_attempts,
        retry_initial_delay_milliseconds=retry_initial_delay_milliseconds,
        retry_max_delay_milliseconds=retry_max_delay_milliseconds,
    )
