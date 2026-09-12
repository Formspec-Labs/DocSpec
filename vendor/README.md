# Locally qualified dependencies

The optional SpicyDocs integration and its source-reading and bill-acquisition
tests use one wheel. `spicy_docs.json` records its version, source revision and
SHA-256; `pyproject.toml` and `uv.lock` select that same file. These dependency
wheels are not bundled inside DocSpec's built wheel.

When accepting a new SpicyDocs release, replace the wheel and manifest, update
the package pin and lock, and run the source-catalog, GovInfo bill and package
boundary tests. Retain acquisition as an optional provider extra. See
[installation](../CONTRIBUTING.md#install-the-optional-source-reader) and the
[bill example](../docs/govinfo-bill-example.md).
