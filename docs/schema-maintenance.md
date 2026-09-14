# Schema and fixture maintenance

Schema bytes participate in artifact identity. Begin by identifying the format
and governing [decision](decisions/README.md). A refactor preserves
existing schema bytes and identities unless the behavior changes intentionally.
For a deliberate format change, update current producers and readers together.
Legacy readers and fixture reproduction are not requirements.

Catalog rows use the required `jsonschema-rs` validator for acceptance and
structured rejection messages through `adapters/schema_validation.py`.
`tests/test_source_catalog_rows.py` checks real rows and invalid mutations.
Supplied schemas use the explicit Draft 2020-12 binding, a frozen reference
snapshot, and an optional base URI. Format assertions must be enabled explicitly;
catalog schemas retain their annotation-only format behavior. The schema adapter
does not retrieve missing references from the network.

Core version 1 records use `msgspec` declarations and generated structural
schemas. Their admission gateway decodes canonical JSON with the shared Rulespec
decoder, converts strictly to the declared types, and applies record-local
semantic checks. Cross-record validity remains with the ledger admission owner.
Payload schema checks follow JSON value-domain admission; a schema cannot widen
the supported numeric or Unicode domain.

## Find the authoritative definition

| Family | Edit and generate | Installed location under `docspec/schemas/` | Checks |
| --- | --- | --- | --- |
| Source catalog 1.0 | `source_catalog_schemas()` in `src/docspec/domain/source_catalog.py`; serialize with `canonical_json_file_bytes` | `source_catalog/1.0/` | `tests/test_package_boundary.py` compares all three files byte for byte with domain generation and checks the wheel |
| Core 1 | Types in `src/docspec/domain/core.py`; `record_schema()` in `src/docspec/domain/core_admission.py` | Generated on demand; no copied schema files | `tests/test_core_records.py` compares generated schema and typed admission on structural acceptance fixtures |

Runtime composition chooses concrete adapters directly. Their configuration and
Core definitions live with the implementation; there is no separate installed
machine-profile registry to regenerate.

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

uv run --frozen pytest tests/test_schema_validation.py tests/test_package_boundary.py
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
