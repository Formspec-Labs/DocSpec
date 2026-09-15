# Reuse processing results beside a catalog

DocSpec keeps a processing result once. A retained `Selection` links that result
to the exact source state, member key and occurrence. `open_selected_outputs`
reads those links and the selected JSON values without writing another dataset.

```python
from contextlib import closing
from docspec.runtime import CoreWorkspace

with CoreWorkspace("workspace", create=False) as workspace:
    with workspace.open_selected_outputs(
        "catalog-revision", definition_id="my-operation-definition",
        output_labels=("identifiers",),
    ) as reader:
        with closing(reader.rows()) as rows:
            for row in rows:
                print(row.origin.member_key, row.origin.parent_entity_id, row.value)
        print(reader.state_pin, reader.definition_pin, reader.pin)
```

Choose the exact operation definition and output labels. The reader checks the
source member and occurrence, retained result evidence, current dependency
correspondence, and retained JSON bytes. Outputs outside the selection's own
labels remain hidden. A member without a matching choice contributes no rows.

Each row carries its original `Origin`, `selection_id`, `result_id`, `output_id`,
`output_pin`, `label`, and decoded `value`. The output pin is its admitted record
digest. The reader's selection-set pin also binds the source state, definition,
selected identities and output record digests; it does not hash all values again.
Pass `expected_state_pin` and `expected_pin` when reopening an exact prior choice.
The selection-set pin becomes available only after complete traversal.

Keep the reader open while using its streams. Rows are provisional until the
stream finishes: a later unavailable result, changed dependency, or wrong pin
refuses the read. Stage disposable index input before adopting it. A second
matching selection stays visible; the consuming product decides whether equal
results can be combined and must handle competing choices explicitly.

`reader.source` is the already-admitted original `CoreStateReader`. Use its
`value_relation`, `values`, or `read_value` inside this same context to join or
retrieve original values without admitting the source state a second time.

Missing retained evidence raises `IntegrityError`; stale source/dependency or
selection-set evidence raises `StaleBaseError`. Opaque bytes raise
`StateValueRelationUnavailable`; choose a JSON processing output instead.
Selected state outputs are outside this reader's scope.

Discovery scans retained selection metadata in bounded batches. Membership
checks share one source admission and use bounded groups of member addresses.
Existing Core bounds apply to input and output content. This does not impose a
total catalog-size limit or promise constant execution time.
