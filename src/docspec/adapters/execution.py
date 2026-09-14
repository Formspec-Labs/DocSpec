"""Bounded local workers for Core operations."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from docspec.adapters.streams import owned_iterator


def worker_capacity(max_workers, max_in_flight):
    for name, value in (("max_workers", max_workers), ("max_in_flight", max_in_flight)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    return min(max_workers, max_in_flight)


def bounded_map(worker, items, *, max_workers=1, max_in_flight=1):
    """Execute Core work with bounded in-flight calls and owned input closure."""
    capacity = worker_capacity(max_workers, max_in_flight)
    pending, exhausted = set(), False
    with owned_iterator(items) as source, ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="docspec-work") as executor:
        try:
            while pending or not exhausted:
                while not exhausted and len(pending) < capacity:
                    try:
                        item = next(source)
                    except StopIteration:
                        exhausted = True
                    else:
                        pending.add(executor.submit(worker, item))
                if pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        pending.remove(future)
                        yield future.result()
        finally:
            for future in pending:
                future.cancel()
