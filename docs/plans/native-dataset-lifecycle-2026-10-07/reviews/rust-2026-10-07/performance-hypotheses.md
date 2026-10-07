# Independent performance review hypotheses

Recorded after reading README.md at 40e891ccece79495f7ab850d441ed8633be73449, before reading the historical REVIEW.md or other reviewers' reports.

H1 (medium): Arrow plus independently updated column groups can reduce payload rewrites, but immutable occurrence/value identity and independent evidence qualification may force full-row/full-history work absent a bounded correspondence/index representation. Test through schema/identity plans before deciding.

H2 (medium): A metadata-only unchanged-input decision is valid inside admitted trust scope, but publication and independent readback may make time-to-verified-release linear in unchanged bytes. Test explicit release-verification reuse requirements and distinguish expected trust-boundary work from unplanned costs.

H3 (medium): Broad lifecycle guarantees create catalog and retention-history growth that dominates small updates even with bounded payload processing. Test retained root enumeration, completed-phase lookup, change composition, correspondence, and old snapshot read requirements at increasing history.

H4 (medium): Shared byte accounting plus DataFusion spilling is not by itself a whole-process memory bound across decoders, Arrow queues, Lance writers, and transaction preparation; evaluate whether a concrete allocation/refusal seam is a missing decision or already scheduled qualification.

H5 (low): Requiring full greenfield Rust adoption before workload qualification may precommit to a costly rewrite without quantified user benefit; compare bounded component gates, alternatives, and kill criteria with RESEARCH and RUST-REWRITE.

H6 (medium): Generic dependent refresh may hide a query maintenance engine in DocSpec. Determine whether supported operation algebra, relationship discovery, and full-recompute fallback already bound this responsibility.

No benchmarks or runtime claims are authorized for this review. No repository changes.

## Updates after tracing R1-R7, research, Core, and primary component documentation

H1 REFINED: R3 lines 13-35 already separate fields/occurrences/values, require qualified correspondence, and compare composition with wide-table cost. R4 lines 42-48 acknowledge full key scans and admission despite delta writes. Physical correspondence and chosen granularity remain a correctly scheduled qualification decision, not a proved optimization.

H2 REFUTED as a missing architecture decision: R1 line 40 and R2 lines 17-19 explicitly preserve public readback as a separate trust boundary; R2 line 44 and R7 lines 81-87 measure cold reader work separately. Metadata-only reuse never claims constant-time public reading.

H3 REFINED: R6 lines 25 and 53, R7 lines 81-87 explicitly address shared roots, file/history scaling, and retained-byte cost. Lance's current flat-manifest format description establishes a concrete linear-in-fragment-count candidate cost; its fragment-tree specification is a possibility, not proof of support in an unselected release. Require pinned feature/API evidence and a fragment/history cross-product; no reason to invent a custom manifest tree now.

H4 REFINED: README line 82 and R7 line 21 acknowledge whole-process versus engine bounds. DataFusion execution 55.1.0 MemoryPool documents that flowing batches and DataSourceExec allocations are untracked. Arrow array 60.0.0 RecordBatch docs document shared-buffer accounting caveats. This is a concrete component-integration risk inside an existing gate; no claim that the plan promises DataFusion alone supplies a hard process bound.

H5 REFUTED as precommitted broad adoption: README lines 3, 33-43, 137, 149, 153 and R7 lines 17, 87, 91 leave selection, performance, and targets open; RUST-REWRITE line 134 puts broad rewrite after evidence. One remaining optional wording correction: the proposed lifecycle comparisons cannot isolate a Rust-language causal effect unless algorithm/layout/work are controlled.

H6 REFUTED: R5 line 17 explicitly limits initial rules to equality joins, replacement, affected-group recomputation, with full rebuild/refusal; line 23 bounds contributor evidence and admits hot-group full scope. General incremental SQL is explicitly deferred.

Historical REVIEW.md was read only after initial hypotheses and all R1-R7 plans. Its approval is lineage, not evidence for this verdict. No other review reports were accessed.
