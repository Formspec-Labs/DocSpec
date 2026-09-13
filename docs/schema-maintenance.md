# Schema and fixture maintenance

Schema bytes participate in artifact identity. Begin by identifying the format
and governing [decision](decisions/README.md). A refactor preserves
existing schema bytes and identities unless the behavior changes intentionally.
For a deliberate format change, update current producers and readers together.
Legacy readers and fixture reproduction are not requirements.

Catalog rows use the required `jsonschema-rs` validator directly. Python
`jsonschema` supplies the detailed rejection messages; it is not a fallback
execution mode. `tests/test_source_catalog_rows.py` compares both engines on
real rows and mutations. Keep DocSpec's schemas and error meaning here; the
existing libraries already provide validation mechanics.

## Find the authoritative definition

| Family | Edit and generate | Installed location under `docspec/schemas/` | Checks |
| --- | --- | --- | --- |
| Source catalog 1.0 | `source_catalog_schemas()` in `src/docspec/domain/source_catalog.py`; serialize with `canonical_json_file_bytes` | `source_catalog/1.0/` | `tests/test_package_boundary.py` compares all three files byte for byte with domain generation and checks the wheel |

Profiles under `src/docspec/storage_profiles/` are maintained machine descriptions
in `docspec-storage-profile` format `2.0`, not generated schemas. The format
records concrete settings and omits unenforced governance labels.
`tests/test_machine_files.py` checks their implementation strings,
roles, references, and conformance mapping. Update descriptions deliberately
when supported behavior changes, not merely because an internal file moved.

## Regenerate generated schemas deliberately

From the repository root, after editing the domain definitions:

```sh
uv run --frozen python - <<'PY'
from pathlib import Path
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.source_catalog import source_catalog_schemas

root = Path('src/docspec/schemas/source_catalog/1.0')
for name, schema in source_catalog_schemas().items():
    (root / name).write_bytes(canonical_json_file_bytes(schema))
PY

uv run --frozen pytest tests/test_machine_files.py tests/test_package_boundary.py
```

Review the JSON diff and its identity consequences. Do not run generation to
hide an unexplained mismatch. The dependency lock and vendored artifact package
are also reviewed inputs; updating dependencies can alter canonical encoding or
structural checks and warrants the equivalence and installed-wheel tests.

## Test the current formats

Result exports use Rulespec's generic container and DocSpec's existing typed
records and evidence checks. Their small product index is checked by
`adapters/result_export/reader.py`; it has no second copied schema engine.
Run `tests/test_result_export.py` and `tests/test_result_export_admission.py`
when changing export meaning or layout. Resealed invalid examples exercise
DocSpec semantics after the shared container's byte checks pass.

The retired campaign-specific portable format, floor calibration and sealed
fixture restamper are preserved in Git history. Their schemas and fixture trees
are not installed or maintained. Current runtime integrity and canonical JSON
tests remain independent of that old format.
