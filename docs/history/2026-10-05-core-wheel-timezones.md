# Core wheel timezone dependency

DocSpec 0.12.4 declares `pytz` beside DuckDB. DuckDB's Python fetch of
`TIMESTAMPTZ` requires it; optional Dagster previously supplied it accidentally.
A minimal 0.12.3 wheel installation cannot fetch a timezone-aware timestamp.
The existing removal-put refusal test also receives that dependency error
instead of the intended `removal repeats` diagnostic.

The installed-wheel package test now fetches an exact timezone-aware value in
its fresh environment, which explicitly excludes Dagster. It failed before
the dependency declaration. No storage code, schema, source policy or existing
state identity changed. The patch version marks corrected installation behavior;
0.12.3 wheel bytes remain unchanged.

Reproduction logs, wheel hashes, gates and independent review live under
`/Users/mikewolfd/Work/corpora/search-delivery-20261005/`. Public package
publication remains separate from local wheel qualification: no registry is
configured and the public PyPI `docspec` project is unrelated.
