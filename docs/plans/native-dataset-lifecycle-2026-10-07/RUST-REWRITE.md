# Greenfield Rust rewrite: performance architecture

Date: October 7, 2026. Status: design proposal; component choices and performance gains require qualification.

**I’d rebuild DocSpec as an incremental columnar engine whose basic units are reusable data fragments, independently updated field groups, and durable processing results.** Rust would make memory ownership and concurrency easier to control; the larger gains would come from changing how much work each operation requires.

The research supports that priority: one reported regulatory run spent roughly 212 seconds deriving results and 2,608 seconds writing and admitting them. That identifies where to investigate, although it does not establish the cause or predict a rewrite’s speedup. [Research findings](/Users/mikewolfd/Work/corpora/session-reports-2026-10-07/data-processing-interviews/FINDINGS.md:21)

I would make these structural choices.

**1. Organize storage around both rows and independently changing columns.**

A Comments dataset would have separately stored groups such as:

- Source fields and original text.
- Extracted or normalized fields.
- Classifications and other enrichments.
- Evidence references and processing outcomes.

Group fields by shared processing and update behavior. A dataset version would select an exact, consistent combination of these groups.

Adding a classification column would read its required inputs and write its results. Existing columns would retain their stored bytes and identities. Backfill coverage would explicitly distinguish “not processed yet” from a legitimate null result.

**Lance would be my first storage candidate to benchmark.** Its format supports versioned fragments containing separate column files, allowing column backfills without rewriting existing columns. That directly fits this lifecycle. Its storage row IDs would remain distinct from DocSpec’s source-occurrence and immutable-value identities. [Lance table format](https://lance.org/format/table/)

I would compare a pinned release against an Iceberg/Parquet baseline before selecting it, including consumer compatibility, selective reads, compaction, recovery, and retained-history costs.

**2. Store evidence once and reference it efficiently.**

Evidence should have two representations serving different needs:

- **Original evidence:** immutable source bytes, receipts, diagnostics, and failed-attempt records.
- **Queryable relationships:** compact columnar records connecting values and processing results to that evidence.

An enrichment should add its own outcome and references to prior evidence. It should not need to embed the entire previous processing history inside every new receipt.

Where many rows share the same input selection, processor, or validation result, store that shared information once. Retain individual relationships where the evidence actually differs. Identical bytes can share storage while separate source observations retain their identities.

This targets repeated serialization, receipt growth, memory allocation, and transfer volume while preserving the ability to reconstruct a complete explanation.

**3. Use one columnar representation throughout execution.**

The processing path would be:

```mermaid
flowchart LR
    A[Pinned inputs] --> B[Resolve reusable results]
    B --> C[Plan affected rows and fields]
    C --> D[Stream column batches]
    D --> E[Write data, evidence and changes]
    E --> F[Commit completed phase]
    F --> G[Publish and verify]
```

Apache Arrow batches would move between readers, transformations, validation, and writers. Operators would request only the columns they need and share buffers where possible. Parsing, decompression, and some transformations still require allocations; those costs should be visible.

I would start with **DataFusion for relational execution**. It already provides a Rust query engine built around Arrow, with planning and columnar execution. Its streaming interface lets downstream consumers pull batches incrementally. [DataFusion](https://datafusion.apache.org/), [streaming execution](https://datafusion.apache.org/library-user-guide/using-the-dataframe-api.html)

Within a phase, compatible operations would run together to avoid intermediate files. Expensive recovery boundaries would deliberately retain outputs. That balances throughput against the cost of restarting.

**4. Make “what needs to run?” a first-class planning step.**

Before opening large payloads, the planner would resolve:

- Exact input versions and evidence.
- Required fields and affected partitions.
- Material processing and validation dependencies.
- Qualified completed work available for reuse.
- Remaining computation, reads, writes, and checks.

A trusted, previously admitted immutable input should support a metadata-only reuse decision. Unknown or mutable input still needs admission.

Writers would produce change information while applying updates: affected keys, fields, and before/after references. Dependent refresh would use those changes plus relationship indexes to find affected outputs—including newly possible join matches.

A changed aggregate may require recomputing its entire affected group. A global transformation may require a full scan. The planner should expose that scope and its reason.

**5. Keep the transactional catalog small and the data work parallel.**

The catalog would hold version selections, input coverage, phase completions, release intents, and retention roots. Large row populations and evidence relationships would live in columnar storage.

Workers could independently prepare immutable outputs. A short catalog transaction would verify the expected version and active processing definition, then make the completed result selectable.

That gives us:

- Parallel preparation without a transaction spanning a long scan.
- Indexed lookup of reusable results.
- Recovery from a completed phase.
- Consistent readers pinned to immutable versions.
- History that shares unchanged objects.

Publication would reuse already available objects, with durable release protection before changing an external pointer. Consumer-specific exports would be retained derived results, so requesting the same export would not recreate it unnecessarily. Disclosure rules would still determine whether native objects are suitable for publication.

**6. Treat memory and concurrency as shared resources.**

I would start with an embedded Rust engine and thin CLI/API entry points. Keep processing stages in one process wherever practical.

All concurrent work would share resource accounting:

- Queues bounded by bytes, including large text and receipt batches.
- Coordinated limits for processing, decoding, writing, and remote requests.
- Spill support for eligible operations.
- Whole-process memory and temporary-storage measurements.
- Cancellation that preserves committed phase results.

Increasing worker count should require available capacity. Separate libraries must not each assume they own every CPU core and the full memory allowance.

The repo could stay compact:

| Boundary | Responsibility |
| --- | --- |
| Model | Identities, schemas, evidence meaning, reuse rules |
| Catalog | Atomic selections, completed phases, retention |
| Storage | Native table format, immutable objects, pinned readers |
| Engine | Planning, batch execution, resource accounting |
| Application | CLI/API, progress, inspect, resume, publication |

Use shared implementations for common rules and narrow adapters around storage and external services. Keep Arrow visible at the internal execution boundary so abstraction layers do not force conversion.

The resulting lifecycle should have these measurable properties:

| User action | Intended work |
| --- | --- |
| Add comments | Process new inputs and affected dependencies; share existing fragments |
| Correct comments | Update affected data, evidence, and dependent results |
| Add an enrichment column | Read required inputs; write the new field group |
| Change evidence only | Update evidence and affected qualifications; reuse eligible values |
| Retry after transfer | Resume from the durable transfer result |
| Read an older version | Open its pinned snapshot directly |
| Publish a small update | Transfer missing eligible objects and verify the release |
| Compact storage | Improve physical layout while preserving logical results |

**The first implementation should prove these properties on the complete Comments lifecycle.** Hold a small update fixed while increasing dataset size and retained history. Measure bytes scanned, decoded, written, and transferred; requests; memory; scratch space; and time through verified publication. Include cold runs, unchanged reruns, and failures between phases.

That would show whether the new structure makes small changes inexpensive—and whether Rust adds further execution gains—before committing to a broad rewrite.
