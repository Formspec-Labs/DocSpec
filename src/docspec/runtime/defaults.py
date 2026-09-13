"""Shared explicit defaults for the local Python and command-line runners."""

from docspec.domain.execution import ExecutionLimits


def local_execution_limits(
    *,
    worker_count: int = 1,
    max_in_flight: int | None = None,
    max_task_index_bytes: int = 4 * 1024**3,
) -> ExecutionLimits:
    """Set direct local concurrency and the temporary task-index byte bound.

    Omitting ``max_in_flight`` uses the worker count. Native schedulers use
    their own concurrency settings; every prepared worker enforces the index
    bound independently of its scheduler.
    """

    return ExecutionLimits(
        worker_count=worker_count,
        max_in_flight=worker_count if max_in_flight is None else max_in_flight,
        max_task_index_bytes=max_task_index_bytes,
    )
