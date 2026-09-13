# Core model implementation plan: review

Reviewed 2026-09-13 against `docs/core-model-implementation-plan.md` (465
lines, updated that day) and the refined `docs/core-model.md`. An earlier
draft of this record judged the plan against prior repository decisions;
the owner directed that it be judged on what the best implementation of the
spec looks like. This is that assessment. Library behavior was checked
against the disk-objectstore design page and the spec's own requirements.
No tests ran and nothing was edited.

**Verdict: RECONSIDER.** The plan is strong where it describes semantics and
weak where it chooses tools. It spends most of its length selecting twelve
foundation components, five of them for requirements that do not yet exist,
and almost none on the two mechanisms the spec introduces that no library
supplies: revision resolution (Core §3.3, §9) and projections (§6.1). The
best version of this plan is shorter on tools and longer on those two
designs.

## What the spec actually demands

Reading Core §§3–9 as an engineering brief, an implementation needs:

1. An **immutable content store** with logical identity separate from
   physical bytes (§5.1, §8).
2. A **transactional ledger** for states, operation definitions, executions,
   results, bindings, declared dependencies, reuse associations and PROV
   relations, with atomic publication as the visibility boundary (§5.2,
   §7.3).
3. A **state resolver** that recovers complete membership from a retained
   base plus ordered edits, preserving occurrence identity, multiplicity and
   ordering, with dictionary semantics under the Keyed-State Profile (§3,
   §9).
4. A **projection mechanism**: a definition language, parameters, vectorized
   evaluation, and retention of the projected value or its recovery from a
   retained parent (§5.2, §6.1).
5. A **correspondence index** from operation and dependency fingerprints to
   candidate results, with policy applied separately (§6–7).
6. **Typed records** for DocSpec's own formats and **schema validation** for
   application payloads (§1's "does not prescribe a serialization" still
   requires one concrete format).
7. **Conformance checks** against a reference model, independent of storage
   (§1, §2).
8. **Bounded PROV export** for interoperability (§2, Appendix C).

Items 1, 2, 5, 6, 7 and 8 are solved problems with good libraries. Items 3
and 4 are DocSpec's own. A plan earns its keep by designing 3 and 4 and by
choosing 1, 2, 5, 6, 7, 8 quickly and once.

## What the plan gets right

- **§2's component table cites the spec by section.** It is the seam a
  reader needs.
- **SQLite as the local ledger** with four batch operations
  (`publish_results`, `record_selections`, `find_candidates`,
  `read_dependencies`) is the right shape: transactional, serverless, and
  the interface hides the engine.
- **§4's publication order** (content, recoverability check, metadata
  transaction) and its crash and retry semantics implement Core §5.2–5.4
  faithfully.
- **msgspec for typed records** is the best Python choice on merit: strict
  typed decoding, schema generation from the same types, and the plan
  correctly notes that constructors do not validate.
- **jsonschema-rs for supplied payload schemas** keeps application schemas
  authoritative instead of hand-translating them into types.
- **The three-identity table** (content fingerprint, correspondence
  fingerprint, logical identity) and the candidate index with many results
  per key are the correct reading of §5.1, §7.3 and §7.5.
- **Edit presence separate from value.** An absent replacement and a null
  replacement differ. This is the seed of the state-resolver design.
- **Recording associations outside memoized computation**, and a fresh
  execution always being a new attempt.
- **§8's coverage list** exercises the spec's hardest cases.
- **The closing rule**: native adoption alone is not evidence of anything.

## Findings

### 1. The two novel mechanisms are undesigned (CONCERN)

The state resolver gets one table row and three sentences in §3. The plan
never defines the edit record, the resolution algorithm, or conflict
detection, although these are where the spec's MUSTs concentrate (§3.1
occurrence identity, §3.3 unambiguous addressing and recoverable order, §9.1
insertion semantics, §9.3 completeness and conflicting revisions). The best
plan states, at minimum:

- A state is either materialized (a manifest of occurrence, optional key,
  optional ordinal, value reference) or derived (a base state plus an
  ordered edit set).
- An edit is (edit set, ordinal, operation insert or remove, key or
  addressed occurrence, value reference or explicit null, presence flag).
- Two levels, not one. A partial edit to an occurrence (Appendix A's
  `language` change) first derives a new occurrence entity from the old one;
  that is a transformation with its own provenance (§3.3, §4). Membership
  then changes by inserting the new entity at the key. Keyed membership
  resolution applies insert and remove edits in ordinal order with last
  writer per key, so the result equals base minus removed keys plus inserted
  pairs. PROV-Dictionary fixes that outcome, not the algorithm;
  last-writer-per-key is one algorithm that produces it.
- Two edit sets from one base touching the same key with no declared order
  are rejected (§9.3).
- Ordered, non-keyed states need a stated addressing rule; stable occurrence
  identifiers with ordering as a separate property is the plausible one.
- Materialize a checkpoint after a bounded number of edits (§8).

Projections get no design at all. For the initial JSON-compatible format,
JSON Pointer (RFC 6901, already named in Appendix C) is the obvious
addressing language, but addressing is not the whole design. RFC 6901
leaves error handling to the application, so the plan must state: the
projected value keeps its JSON type, because a naive string extraction gives
the number 1 and the string "1" the same fingerprint, which is a
correspondence error (Polars' `json_path_match` returns String for every
match; DuckDB's `json_extract` returns JSON); absent, null and present are
three outcomes (§6.1); a multi-field projection is an ordered composite with
its own definition; array-index tokens are positional and need a stated rule
under revision (§3.3); and the projected value is canonically encoded before
fingerprinting. Whole value is the degenerate projection. The retained value
is either stored or recovered from the retained parent under §5.2.

**Reshape.** Add a §3 subsection for each. These are the sections a future
implementer will actually read.

### 2. The plan decides a compiled component before any operation needs it (CONCERN)

Line 16 states the correct rule: "Where existing libraries cannot express a
required bulk operation, include a focused native extension in that
operation's first implementation." §1 and §4 then make a Rust component
unconditional: rusqlite through PyO3, pyo3-arrow, maturin, the Rust
`jsonschema` crate, and Polars plugins, before any such operation is named.

On merit the ledger does not need it. SQLite's own write path is the floor;
binding rows in Rust instead of Python saves a fraction of a cost that is
already small relative to the insert. The ledger scales with results and
associations, and §4 says bulk content stays in files the ledger names. The
plan itself enumerates the fixed cost it takes on: cross-language version
pinning, buffer ownership across the bridge, GIL release during waits, and a
second JSON Schema implementation that must agree with the first on drafts,
numeric behavior and error mapping.

Which per-row operations in this spec might genuinely need native code?
Canonical encoding plus hashing of every record, and per-record projection
evaluation. The first is a real candidate and should be measured on the
initial format. The second is vectorized JSON extraction in either engine.
Neither is the metadata ledger.

Measured on this machine on 2026-09-13
(`probes/2026-09-13-sqlite-ledger-binding-probe.py`), corrected the same
day after the plan's author noted that the first baseline let SQLite skip
value formatting in a count-only query. Ledger-shaped rows with a primary
key and one secondary index, one WAL transaction, formatting forced in the
baseline, both durability settings, three runs:

| Rows | SQLite insert floor | Full Python path | Largest possible native saving |
| --- | --- | --- | --- |
| 1,000,000 | 0.74–0.83 s | 1.41–1.49 s | 0.57–0.74 s, 41–50% |
| 4,000,000 | 3.17–5.33 s | 6.21–7.62 s | 1.40–4.24 s, 21–56% |

The Python path is Arrow batch to tuples to `executemany`. Throughput falls
as the indexes grow, so a linear extrapolation from the small run
understates larger ones. At twenty ledger rows per document, a
million-document run spends roughly 35–40 s writing its ledger through
Python, and a native binding could remove at most about half of that, in a
run that spends hours fetching. The first version of this paragraph
reported 27–32% and about 8 s, understating the saving by about half. The
conclusion rests on the absolute magnitude, not the percentage. Limits:
synthetic rows, one index, one transaction per run, warm cache, no
concurrent readers.

**Reshape.** Keep line 16 as the rule. Use the standard-library `sqlite3`
module behind the same four batch operations. Move the Rust component to
§7 with its trigger: an attributed profile of a required batch operation
that expressions cannot express. Name canonical encoding and hashing as the
first candidate to measure.

### 3. Two bulk engines where one is needed (CONCERN)

§1 selects Polars for bulk operations and Arrow for interchange, and lines
42–47 describe DuckDB as existing context to "reuse what fits". The plan
does not select two target engines; an earlier draft of this record said it
did, and that was a misreading. It does leave open whether DuckDB is
retired, and "reuse what fits" can leave both in the codebase. The spec's
bulk operations are all relational: base-plus-edits resolution (join and
coalesce), membership diff (anti-join), projection evaluation (JSON
extraction), fingerprint comparison (join), candidate work (anti-join), and
batch writes. Either engine expresses all of them. The discriminators are
two: out-of-core execution for states larger than memory, where DuckDB is
stronger and the plan hedges Polars ("streaming execution where supported;
verify the actual plan and memory use"), and custom native row functions,
where Polars' expression plugins are the easier path.

Those two decisions are therefore coupled, and neither is settled by
argument: Polars' streaming engine and DuckDB's spill both have documented
limits. The decision procedure is to run the actual resolver and projection
queries on both, including one state larger than memory, and pick one
engine. Running both is the worst outcome: two optimizers, two memory
models, two sets of JSON functions.

**Reshape.** One engine, chosen by measuring the resolver and projection
queries together with the native-function question in Finding 2, and a
sentence saying whether DuckDB is retired. Arrow remains the interchange
either way.

### 4. Two hash algorithms where one is needed (OBSERVATION)

BLAKE3 for correspondence fingerprints and SHA-256 for content digests.
Fingerprints hash small encodings once per request, so speed is
irrelevant there; content digests hash whole documents, where BLAKE3's
speed would actually matter. The choice is inverted on its own logic. One
algorithm means one encoding version and one audit path.

**Reshape.** SHA-256 throughout. The selected content store is SHA-256
only, so "BLAKE3 everywhere" is not available with it, and BLAKE3's gain
here is unproven. One algorithm does not remove separate encodings or
identity meanings; the three-identity table stands.

### 5. §7 pre-selects tools for absent requirements (OBSERVATION)

PostgreSQL, pyoxigraph and oxrdflib, delta-rs, obstore, codecs and plugins
each receive a tool pick and design text. Nothing in the spec requires an
RDF store, a Delta table or a shared database, and Appendix C already calls
these "interoperability directions". A plan that names a tool for every
possible future ties itself to today's library landscape for decisions that
will be made in a different one.

**Reshape.** Keep the principle (additions implement the same interfaces
and pass the same semantic cases) and the Dagster row. Drop the rest to a
one-line list or nothing.

### 6. disk-objectstore: defensible, but say why and name the gap (OBSERVATION)

Packing is the right answer once object counts reach the range where a
filesystem of loose files hurts, and a document corpus at the scale
Decision 0002 describes reaches it. disk-objectstore is a maintained
implementation of exactly that, SHA-256 keyed, with loose writes from many
processes and one packer at a time. The plan should state that
object-count rationale, and should state the gap its design page confirms:
no remote backend, so remote storage is a second adapter behind the same
content interface from day one.

### 7. rustworkx: harmless (OBSERVATION)

The plan does say rustworkx operates on the relevant dependency subgraph,
separate from the historical PROV graph. If that subgraph is the operation
graph, a handful of processors, the library is unnecessary but costless; if
it is per-record, a recursive query in the ledger is the alternative. Not a
decision that matters.

### 8. "Benchmarking is not a prerequisite" is wrong on merit (OBSERVATION)

Lines 19–22 say to choose tools at the outset and benchmark later. Where
two options both plausibly fit, a bounded measurement is how the choice is
made; it is cheaper than a wrong foundation. The plan's own closing
paragraph says so. Delete lines 19–22 and let the closing paragraph govern.

### 9. §8 step 2 is a big bang (OBSERVATION)

Step 2 builds state resolution, identities, correspondence, associations,
native functions, reopen, inspection, failure recording and interrupted
publication "in the same foundation". A sequence that retires risk in order
is: ledger and content store with typed records; capture and derive with
publication and crash tests; correspondence and reuse associations;
revision and the keyed-state resolver; PROV export. Each step has its own
oracle case from the §8 list.

### 10. Vocabulary drift against the spec (OBSERVATION)

§2 says "attempts" and "output bindings"; the spec says operation execution
(§4.1) and result bindings (§4.4). §2 says "qualify discovered omissions";
§6.3 now says the discovered omission must remain recoverable. §2's list
of stored PROV relations omits the Plan and Association binding the spec
gives operation definitions. Three word-level edits.

## Required reshape

1. Add the state-resolver and projection designs (Finding 1). This is the
   plan's missing center.
2. One bulk engine, chosen together with the native-function question
   (Findings 2, 3). Standard-library `sqlite3` behind the four batch
   operations; the Rust component moves to §7 with a trigger.
3. One hash algorithm (Finding 4). Cut §7 to its principle and the Dagster
   row (Finding 5). Delete lines 19–22 (Finding 8).
4. Sequence §8 step 2 (Finding 9). Apply the three vocabulary edits
   (Finding 10).
5. Keep §2, §4's publication semantics, §5's identity table and typed-record
   choices, §6's index, and §8's coverage list unchanged.

## Reconciliation with the plan author's response, 2026-09-13

The plan's author accepted Findings 1, 2 (as a question to settle, not a
verdict), 5, 8 and 9, and disputed four points. Three of the four were
right and are folded in above: the plan selects one target engine, not two
(Finding 3 corrected); JSON Pointer is an addressing language, not a
projection design, and string-typed extraction is a live correspondence
hazard (Finding 1 extended); last-writer-per-key resolves membership only,
and partial occurrence edits compose at the value level first (Finding 1
extended); "BLAKE3 everywhere" is not available with a SHA-256-only store
(Finding 4 corrected). The fourth dispute, that the Rust saving was
asserted rather than measured, was also right, and the measurement in
Finding 2 replaces the assertion. Its result supports the original
conclusion for the ledger and says nothing about the bulk engine, where the
author's proposed method, comparing the actual resolver and projection
queries, is the one to use. The author then
caught a flaw in the measurement itself: the count-only baseline skipped
formatting, and the extrapolation was linear. The corrected probe roughly
doubles the largest possible native saving and shows it shrinking as a
share of a growing cost; neither changes the decision for the ledger.

## Decision, 2026-09-13

The owner directed a decision rather than a deferred comparison. Measured with
`probes/2026-09-13-engine-resolver-probe.py` on identical Parquet inputs, two
million members and forty thousand ordered edits with repeated puts and
removes, each engine in its own process:

| | DuckDB 1.5.5 | Polars 1.44.2 |
| --- | --- | --- |
| Resolve, extract three fields, SHA-256 every row | 0.71–0.76 s | 0.57 s plus 0.72 s hashing through a Python callback |
| Peak process memory | 120–122 MiB | 1,115 MiB; 583 MiB fully lazy without SHA-256 |
| Number 1 versus string "1" preserved | yes | no |
| SHA-256 in the engine | yes, byte-identical to `hashlib` | no |
| New dependency | none | one |

Both resolved the same 2,001,000 members. **DuckDB is the bulk engine**: equal
speed, a tenth of the memory, typed extraction and hashing inside the engine,
and nothing new to install. rustworkx is replaced by the standard library's
`graphlib` for the operation graph and recursive SQL for affected-result
traversal. The plan's §1, §2, §3, §5, §6, §7 and §8 were edited accordingly,
and the stale critique of the ledger probe was replaced with the corrected
result. Not measured: a state larger than process memory, which step 7
qualifies.

## Accepted, 2026-09-13

The owner accepted the four recommendations from the alignment check and
they are applied to the plan: a contextual role and new-or-adopted flag on
every result binding (§2); an established-or-uncertain marker on resource
descriptions that enters the correspondence preimage (§5); a retention
policy record that every removal must cite, with crash cleanup as one such
policy (§2, §4); and a guarded current selection per dataset, moved only
with an expected-current check (§2, §4, §8). The spec needed no change; all
four implement existing Core §4.4, §5.2 and §5.6 text.
