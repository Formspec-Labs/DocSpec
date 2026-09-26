"""The shared native connection settings for record storage and bulk work."""

from pathlib import Path

import duckdb


ENGINE_MEMORY_BYTES = 6 * 1024**3
# One thread is a correctness bound, not only a memory one. Writers stream Python
# iterators through DuckDB's arrow scan; with more threads DuckDB pulls them on its
# workers, and lazy SQLite-backed sources refuse ("SQLite objects created in a
# thread can only be used in that same thread"; tests/test_parquet_arrow_stream.py).
# Four threads would rewrite the Federal Register catalogue's entity layer 2.7x
# faster once that input is pulled on its caller's thread; its key-ordered read
# gains nothing, being bound by Arrow work
# (docs/history/probes/2026-09-25-read-keys-not-payloads.md).
ENGINE_THREADS = 1


def connect(
    scratch: str | Path, *, memory_bytes: int = ENGINE_MEMORY_BYTES,
    scratch_bytes: int = 80 * 1024**3, threads: int = ENGINE_THREADS,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection bounded by memory, scratch bytes and thread count."""

    if min(memory_bytes, scratch_bytes, threads) <= 0:
        raise ValueError("native engine limits must be positive")
    return duckdb.connect(config={
        "threads": str(threads), "memory_limit": f"{memory_bytes}B",
        "temp_directory": str(scratch), "max_temp_directory_size": f"{scratch_bytes}B",
        "preserve_insertion_order": "false", "partitioned_write_max_open_files": "1",
        "partitioned_write_flush_threshold": "2048",
        "parquet_metadata_cache": "false", "validate_external_file_cache": "VALIDATE_ALL",
    })
