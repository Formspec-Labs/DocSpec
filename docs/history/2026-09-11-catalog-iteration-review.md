# D07 catalog iteration: independent static review

**Verdict: APPROVE.** No unresolved material finding in the reviewed source and documentation. D07's bounded acceptance is supported by the implemented paths and the concrete tests described below; root owns execution and the final acceptance record.

This review applied the requested semi-formal code-review skill to the D07 work over the existing September 11 implementation. It did not run tests, build packages, edit repository files, or inspect unrelated historical files. Parent-reported test results are separate from this static verdict.

## 1. Change and scope

The public `preview_local_catalog` reads exact admitted catalogs and explains catalog choices and successive snapshot differences before acquisition. Existing planning now distinguishes source-description changes from changed acquisition inputs, so metadata can refresh without refetching or rerunning compatible processing. No second planner, persistent comparison index, policy router, or scheduler was introduced.

Reviewed: `runtime/catalogs.py` and its export; `application/catalog_preview.py`, `comparison.py`, `inspection_comparison.py`, `planner.py`, `base_reprocessing.py`; `domain/content.py`; `tests/test_catalog_iteration.py` and the planner/prefix test deltas; `docs/catalog-iteration.md`, catalog-input guide links, and the D07 architecture note. Existing processor request and capture identity paths were traced to verify the new equality boundary.

Paths below are relative to `/Users/mikewolfd/Work/DocSpec`.

## 2. Function and dataflow traces

| Entry point | Traced behavior | Evidence |
| --- | --- | --- |
| `preview_local_catalog` | Validate sample limits before reading storage; open current/optional prior exact references with explicit producer acceptance and `create=False`; pass admitted rows and summaries to the application formatter. | `runtime/catalogs.py:75`; existing `open_local_catalog:57` |
| `preview_catalog` | Exhaust full populations; pair by catalog UTF-16 identity order; count all changes independently of bounded samples; report omission as `removedFromCatalog`; separate catalog selection from run filters and actual work. | `application/catalog_preview.py:51`, `:74`, `:107` |
| Shared comparison | Canonical JSON equality distinguishes booleans from numbers; field comparison distinguishes absent from null and escapes JSON pointer keys; paired rows retain one item per stream and register both closures with `ExitStack`. D19 delegates these mechanics while preserving its own signatures and order. | `application/comparison.py:14`, `:20`, `:44`; `inspection_comparison.py:122` |
| Acquisition eligibility | Qualified ID, source-issued version, state, and the full canonical candidate list must match. Only source-level metadata is excluded. | `domain/content.py:214` |
| Existing planner | Apply run selection first. Reuse requires equal acquisition inputs and unchanged non-stage governing policy. Failed items pass the existing explicit/relevant-change admission before any metadata replacement. Healthy changed metadata selects the deepest compatible existing prefix; truly unchanged rows still produce no task. | `application/planner.py:353`, `:357`, `:361`, `:491` |
| Retained prefix | Use the same acquisition comparison, then retain all existing disposition, capture-population, blob, stage-identity, receipt, and processor-result checks. The newly planned entry retains fresh source metadata; inherited captures retain their original provenance. | `application/base_reprocessing.py:84` onward |
| Processing dependency | Current processor requests take qualified source ID, exact segment inputs, prerequisites, allowed fields and limits; reuse identity binds those inputs and the processor description. Source catalog metadata is not passed as a processor input. Capture identity still includes source version. | `application/processor_rules.py:65`; `domain/processors.py:400`; `domain/content.py:44` |

## 3. Invariants and scope limits

The design preserves current source evidence and original acquisition evidence separately. It does not equate matching document IDs across namespaces, ignore changed candidate metadata, bypass artifact verification, or silently retry a held failure. Explicit snapshot omissions retain existing deletion planning subject to run filters. `observed-crawl` remains a coverage description, not an append instruction.

Each returned sample has its own count and canonical-entry byte cap. Summary metadata and report framing remain outside that cap, as the API and guide state. Admission and comparison still read the complete catalogs; this review does not establish large-corpus latency or a whole-report byte ceiling. Existing admitted-reader bounds and trust policy remain the authority.

## 4. Concrete acceptance evidence inspected

- `tests/test_catalog_iteration.py:125`: real policy successor adds one item, excludes one, removes one, and changes a kept item's interpretation. Preview is read-only and causes no fetch. Execution refreshes the kept source row while retaining exact file, representation, segment and derived payloads, with only the added item causing new stage calls. Live inputs/values/configuration compare with a clean run; another identical successor has no tasks.
- `:176`: capture-only, extraction-only and segmentation-only metadata changes select their correct deepest prefix and cause no repeated stage calls.
- `:193`: policy-only metadata change holds an existing permanent failure and its original description; explicit retry repairs through the retained prefix and replaces the description.
- `:211`, `:221`, `:233`: non-stage governing changes remain FULL; observed-crawl omission removes prior state; disjoint same-policy inputs work while repeated qualified IDs refuse.
- `:246`, `:261`, `:271`, `:289`: zero/byte/item sample limits preserve complete counts, empty catalogs work, bad bounds refuse before opening missing storage, late iterator failure is not hidden by sampling, and UTF-16 merge/early closure are exercised.
- `:307`, `:316`: missing/null distinction, escaped keys and a real public preview of nested numeric-to-boolean metadata changes.
- `tests/test_planner.py:495`: numeric-to-boolean candidate metadata requires FULL acquisition. Existing candidate population/locator/digest/size/transport cases remain conservative.
- `tests/test_prefix_reuse.py:93`: changed source version or candidate locator, missing/repeated captures and wrong extraction pins refuse before new receipt writes.

These are test assertions inspected, not tests executed by this reviewer. The author reported root's first 96-test gate passed before the final JSON equality regressions; root should record the final gate separately.

## 5. Findings and resolutions

1. **Resolved — missing versus null.** The old D19 field helper used `.get()` without a sentinel and could omit this difference. The shared helper now emits explicit presence flags; a focused regression covers both directions.
2. **Resolved — JSON boolean versus number.** Initial preview/status logic used Python equality, causing source metadata `1` to compare equal to `true` even though planner identity changed. Shared canonical equality now drives both preview and D19 status/differences. The author also applied canonical comparison to full candidate inputs, preventing unsafe acquisition reuse for that same distinction.
3. **Resolved — failure admission.** Metadata-only replacement now goes through D16's existing frontier/retry decision, preventing a policy explanation from clearing or retrying an unchanged permanent failure. The public runtime case demonstrates the held and explicitly repaired states.

No remaining material correctness, ownership, or documentation finding was found in this slice.

## 6. Acceptance and confidence

D07 is ready for root's final executed gate and completion decision. It delivers previewable catalog growth with qualified identities, explicit collision/omission semantics and unchanged-work reuse. The implementation stays within existing reader, planner and prefix-verification ownership. Confidence is high for this bounded static conclusion.

This does not establish arbitrary mixed-policy merging, publisher-wide completeness, execution-cost prediction, export convenience, live-provider qualification, or Dagster interruption behavior. Those are not claims made by this implementation.
