# Export a selected Core state for independent reading

`CoreWorkspace.export(state_id, destination, producer=..., max_output_bytes=...)`
creates a portable artifact containing the selected state and its required
metadata, content and provenance. Supply `additional_roots` as explicit
`(kind, id)` pairs when the consumer also needs historical selections or results.
The exporter follows the publisher's retention obligations and maintenance's
shared physical inventory; it does not copy unrelated workspace state.

```python
from docspec.runtime import CoreWorkspace
from docspec.result_export import open_result_export

with CoreWorkspace(workspace_path) as workspace:
    pin = workspace.export("selected-state", output_path,
        producer=export_producer, max_output_bytes=256 * 1024**2)

with open_result_export(output_path, expected_pin=pin,
        producer=export_producer, max_output_bytes=256 * 1024**2) as exported:
    for key, occurrence in exported.rows():
        print(key, occurrence.entity_id)
```

The accepted producer and expected pin come from the publisher through a trusted
channel. Rulespec verifies artifact membership and bytes. DocSpec checks Core
records, selected membership, content references and required retained evidence.
The version 2 export pins portable Iceberg snapshots. The consumer does not need
the original workspace, a catalog service or producer implementations. Creating
an export requires the configured catalog to materialize selected rows.
Selected membership is materialized into the export so unrelated values sharing
an old physical source layer are not included accidentally.

Publication uses a private working directory and exclusively publishes the
finished destination. Existing matching output can be admitted and reused;
a conflicting destination refuses. The byte allowance covers the exported
artifact. Interrupted publication can leave unreferenced temporary files, but
those files are not an admitted export.

Use `docspec export --help` for state, producer, destination, byte limit and
additional-root inputs. The [writer](../src/docspec/adapters/result_export/writer.py),
[reader](../src/docspec/adapters/result_export/reader.py),
[export tests](../tests/test_result_export.py) and
[admission tests](../tests/test_result_export_admission.py) cover independent
reading, byte limits, tampering, selected closure and retained history.

Application-specific requirements, such as nonempty text or usable search
records, belong in explicit application checks. This Core artifact does not
silently filter its selected population to satisfy those requirements.
