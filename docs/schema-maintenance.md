# Schema and fixture maintenance

Schema bytes participate in artifact identity. Begin by identifying the format
and governing [decision](decisions/README.md). A refactor preserves
existing schema bytes, field order, IDs, digest domains, and sealed fixtures.
An intentional format change needs its own compatibility decision and review.

## Find the authoritative definition

| Family | Edit and generate | Installed location under `docspec/schemas/` | Checks |
| --- | --- | --- | --- |
| Source catalog 1.0 | `source_catalog_schemas()` in `src/docspec/domain/source_catalog.py`; serialize with `canonical_json_file_bytes` | `source_catalog/1.0/` | `tests/test_package_boundary.py` compares all three files byte for byte with domain generation and checks the wheel |
| Scale profile 2.0 and scale result 1.0 | Dataclasses in `domain/scale.py` plus explicit constraints in `tools/generate_scale_profile_schema.py` | `scale_profile/2.0/`, `scale_result/1.0/` | `tests/test_machine_files.py` compares generator output, `conformance/` copies, and installed-source copies; package-boundary checks inspect the wheel |
| Portable DocumentRelease 2.0 | Deliberately edit the eight JSON schemas in `src/docspec/schemas/document_release/2.0/` against Decision 0001; these are not generated from `domain.release.DocumentRelease` | `document_release/2.0/` | `tests/test_document_release_schema_bundle.py`, the five focused verifier suites in the command below, `tests/test_document_release_builder.py`, and fixture restamp check |
| Retention-floor calibration 2.0 | Deliberately edit `src/docspec/schemas/retention_floor_calibration/2.0/retention-floor-calibration.schema.json`; keep calibration writer and verifier aligned | `retention_floor_calibration/2.0/` | `tests/test_retention_floors.py`, format and per-kind checks in `tests/test_document_release_wire_format.py` and `tests/test_document_release_text_bodies.py`; `tools/calibrate_retention_floors.py` loads the packaged schema |

Profiles under `src/docspec/storage_profiles/` are maintained machine descriptions, not generated
schemas. `tests/test_machine_files.py` checks their implementation strings,
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

uv run --frozen python -m tools.generate_scale_profile_schema > conformance/scale-profile.schema.json
cp conformance/scale-profile.schema.json src/docspec/schemas/scale_profile/2.0/scale-profile.schema.json
uv run --frozen python -m tools.generate_scale_profile_schema --result > conformance/scale-result.schema.json
cp conformance/scale-result.schema.json src/docspec/schemas/scale_result/1.0/scale-result.schema.json
uv run --frozen pytest tests/test_machine_files.py tests/test_package_boundary.py
```

Review the JSON diff and its identity consequences. Do not run generation to
hide an unexplained mismatch. The dependency lock and vendored artifact package
are also reviewed inputs; updating dependencies can alter canonical encoding or
structural checks and warrants the equivalence and installed-wheel tests.

## Preserve sealed fixtures and their provenance

`tests/fixtures/document_release_v2/` is the frozen predecessor corpus. Its
embedded schemas and tree seals preserve the original bytes; the production
verifier no longer accepts that shape. The restamper never writes it.

`tests/fixtures/source_catalog_release_v1/valid/` remains an input to the current
restamping recipe. Its historical name does not make that input unused. Keep
the recipe and provenance together when reviewing future fixture changes.

`tests/fixtures/document_release_v2_docspec/` is the supported portable corpus.
To check it without changing committed files:

```sh
uv run --frozen python tools/restamp_document_release_fixtures.py --check
uv run --frozen pytest tests/test_document_release_schema_bundle.py \
  tests/test_document_release_verify.py tests/test_document_release_identity.py \
  tests/test_document_release_wire_format.py tests/test_document_release_text_bodies.py \
  tests/test_document_release_member_index.py tests/test_document_release_builder.py \
  tests/test_canonical_encoding_equivalence.py
```

`--check` rebuilds into a temporary tree and compares the sealed corpus manifest,
including the rebuilt cases' digests. Verifier tests separately check committed
bundles and their exact diagnostic sets. A changed acceptance rule requires
review of accepted and rejected cases, not just a new valid sample.

Only after deciding to change the wire format or its sealed test cases, use:

```sh
uv run --frozen python tools/restamp_document_release_fixtures.py --allow-regeneration
```

This replaces the current portable corpus. Review every changed tree digest,
root identity, embedded schema, expected diagnostic, and generated member. Keep
the predecessor corpus untouched. Record why new bytes are required and which
consumers or published pins are affected; passing regenerated tests alone does
not establish compatibility.
