# Retry failed work while keeping completed inputs

A retained result can contain accepted failures. Use it as an explicit base for
a later experiment. DocSpec keeps the original result and its failure evidence;
the new attempt records the work it performs and the completed inputs it reuses.
Rejected runs are not eligible retained bases.

## Choose which failures to retry

Pass retry intent through the existing selection setting:

```python
with prepare_local_experiment(
    catalog_reference, workspace, **settings,
    base_release=failed_result,
    selection={"retryFailures": "transient"},
) as retry:
    repaired_result = retry.retain(retry.run())
```

`settings` supplies the same explicit limits, accepted producers, timestamp,
deadline, and selected implementations described in [Python runs](python-runs.md).
The ordinary selection filters still choose the source population first.

| `retryFailures` | What it admits for unchanged failed items |
| --- | --- |
| `none` (default) | No automatic retry. Relevant input/settings changes or removing unfinished work can still admit a repair. |
| `transient` | A final temporary external or resource failure. |
| `selected` | Any accepted failure in the selected population, including deterministic failures. |

Combine `selected` with `includeItemIds` to retry particular source items. These
are the catalog's qualified source item IDs, available through catalog reading
and inspection. Changing retry selection does not invalidate healthy items.
`RetryPolicy` separately controls repeated attempts inside each invocation.

The final failure matters. Historical timeout rows do not make a later permanent
failure temporary, and earlier failed attempts do not make a successful result
failed. Each disposition now records `terminalFailure`; successful results use
null. This is disposition schema `docspec-disposition-record/3.0`, with no legacy
reader for the superseded shape.

## What is reused

Planning examines each failed item's own saved stage settings and completion
receipts. It records the chosen execution mode and processor subset before any
work begins. Execution verifies the actual retained bytes and selected
implementations again before reuse.

- Complete captures can be reused after extraction fails.
- Complete representations can be reused after segmentation fails.
- Complete segmentation can be reused after a processor fails, including valid
  empty segmentation proven by its receipt.
- Complete unaffected processors can be reused. An incomplete processor is
  scheduled again for its required segments; an individual invocation may still
  use a verified exact-input cache result.

DocSpec reuses complete stages, not arbitrary unfinished fragments. Contradictory
or damaged promised evidence refuses; it does not silently become a full rebuild.
Source changes and governing policy changes continue to schedule full work.

Changing independent processor B does not automatically retry permanent failure
A. That whole item's previous output, stage settings, and failure remain visible.
Change A or its relevant inputs, explicitly select its failure for retry, or
remove its unfinished work. Removing a failed processor or stopping before a
failed stage can retain a successful prefix. A failure after every requested
output completed can also be explicitly retried using those complete outputs.

## Check the result and its costs

[Inspection](inspection.md) separates planned work, reused stages, current
invocations, cache origins, and the complete retained result. A repaired item does
not inherit old failures or old attempt charges into its new accounting. Its
original result still exposes that history. Recovery restores saved current
invocations once and preserves the planned execution mode and processor subset.
Work lost before a durable checkpoint is not claimed as measured cost.
