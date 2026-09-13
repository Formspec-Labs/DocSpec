# DocSpec Core implementation plan

Updated 2026-09-13. This plan implements the [Core model](core-model.md) on one
local foundation. It describes planned work; implementation checks establish
conformance and performance. The [implementation tasks](core-model-implementation-tasks.md)
break it into dependent deliverables with completion checks and code to retire.
The [consensus record](history/2026-09-13-core-model-consensus.md) says what
was decided, by whom, and on what evidence.

Every component here is needed for the first complete implementation. Nothing
is deferred behind a trigger. A capability that later turns out to be needed is
a new decision with its own record.

Legacy support is out of scope: adopt the new APIs and storage formats directly,
update owned consumers, and remove superseded paths. Build no old-format importer,
compatibility layer, migration framework, or dual-write path. Preserve useful
document workflows; retention and recovery apply to data admitted by the new
implementation.

Keep the public API, operation definitions, job coordination, and decisions made
once per operation or batch in Python. Use DuckDB and Arrow for bulk state
resolution, selected-value evaluation, and comparisons. DocSpec owns these rules
regardless of where they execute. Use bounded batches across that boundary, keep
bulk data in native structures, avoid repeated scans and conversions, and batch
queries and publication. Bounded Python decoding, validation, and canonical
encoding remain explicit costs to measure. There is no DocSpec native component.

## 1. Implementation foundation

| Responsibility | Selected component | Scope |
| --- | --- | --- |
| Public API and operation coordination | Python | Describe operations and policy, coordinate batches, and expose results and errors. |
| Typed records and general JSON processing | msgspec | Decode and validate fixed requests, definitions, manifests, and interchange records; encode ordinary JSON and generate schemas from the record types. |
| Supplied JSON Schema validation | jsonschema-rs | Compile and reuse supplied schemas; validate at admission with explicit draft, reference, and format settings. |
| Authoritative metadata | SQLite through standard-library `sqlite3` | One backend owns SQL, connections, transactions, schema initialization and version checks, and bounded row binding behind the six operations in §4. |
| Local artifact content | The existing content-addressed blob store | SHA-256 keyed, put-if-absent, streamed, with its S3 adapter. The ledger owns logical identity; the store owns bytes. |
| Bulk catalog and dataset operations | DuckDB | Resolve retained states and selected values, compare dependencies, and identify candidate work in SQL over Parquet and Arrow. Typed JSON extraction and SHA-256 are built in. Decided by the §3.3 measurement. |
| Columnar interchange and Parquet access | Arrow / PyArrow | Exchange bounded batches and streams; retain opaque payloads alongside queryable columns. |
| Dependency graph algorithms | Standard-library `graphlib`; recursive SQL | Cycle checks and topological order for the operation graph; traversal of potentially affected results as a recursive query over the ledger. |
| Content and correspondence hashing | SHA-256 | Separate, versioned encodings for content and correspondence; distinct logical entity identities. |
| Local execution | Python functions | One DocSpec lifecycle. Dagster is the optional job adapter in §7. |
| Behavioral checks | pytest + Hypothesis | Check revisions, retention, failure, and reuse sequences against a simple reference model. |

The [current architecture](architecture.md) and [record storage](record-storage.md)
describe the existing DuckDB/PyArrow runtime and storage adapters. The target
keeps DuckDB as the sole bulk engine and the existing blob store as the content
store. msgspec and Hypothesis are the only new dependencies.

The deliverable is one local repository with complete Core behavior, one
versioned record format, explicit dependencies, JSON values admitted by that
format, and opaque binary artifacts. Root values may be scalar or structured.
The initial binding is keyed: every state is a Keyed-State Profile dictionary,
and conformance to that profile is declared when its semantics are implemented
and checked. The workflow runs without a database server, scheduler service, or
RDF store.

## 2. What DocSpec implements

| Component | Responsibility | Model reference |
| --- | --- | --- |
| State resolver | Recover complete states from retained bases and ordered edits; preserve occurrences, multiplicity, and keys. | Core §3; Keyed-State Profile §9 |
| Selected-value evaluator | Recover defined projections with their types, presence, and origin; produce the evidence used for correspondence. | Core §§5.2, 6.1, 7.1 |
| Operation records | Distinguish definitions, input bindings, operation executions, outcomes, and result bindings, each binding carrying its contextual role (raw or derived) and whether the entity is new or adopted; preserve the required PROV interpretation. | Core §§2, 4 |
| Retention publisher | Check required inputs, outputs, descriptions, and provenance before publishing successful retention. | Core §5 |
| Correspondence and policy | Account for material dependencies and retain discovered omissions and corrections; separately apply freshness and execution policy. | Core §§6–7 |
| Result-selection associations | Record the target context, requested operation and inputs, and the particular retained result selected. | Core §7.3 |
| Current selection | Keep one guarded current pointer per dataset naming a retained state or result; move it only with an expected-current check; never rewrite states or results. | Core §5.6 |
| Retention policy | Authorize every removal of retained content or records through a cited policy record; nothing else deletes. | Core §5.6 |
| Conformance checks | Test DocSpec requirements and the recoverable PROV interpretation against the ledger, independently of physical storage. | Core §§1–9 |

Keep dataset meaning in these owners. SQL, object storage, and executors supply
their existing mechanisms. Semantic ownership does not require Python execution:
the same rules govern SQL checks and Python code. Map the current runtime to the
Core terms before adding parallel implementations of the same behavior.

The metadata component owns result lookup, publication conditions, and durable
associations. DuckDB computes changes and comparisons over dataset content;
`graphlib` and recursive queries supply dependency algorithms. Pass their results
through the metadata operations for publication. Keep each rule in one owner and
share its batch implementation between direct execution and the job adapter.

Store PROV entities, activities, membership, usage, generation, and established
derivation with the DocSpec-specific records using bulk reads and writes. Preserve
the Plan of an Association interpretation for separately identified operation
definitions, as required by Core §2, without inventing unknown agents or activities.
Preserve the distinction between actual execution relationships and conservative
dependency descriptions. Returning an existing artifact preserves its original
generation and provenance. Use shared records or manifests where they preserve
the required distinctions; Core does not require a database row or task for
every logical entity or association. The PROV interpretation is recovered from
these records by the conformance checks; no export library is part of the plan.

## 3. State resolution, selected values, and bulk execution

The following records and algorithms define the initial JSON binding. They are
implementation choices under Core, not new requirements imposed on other
bindings. Known-answer fixtures fix their observable behavior before implementation.

### 3.1. Immutable revisions and complete state recovery

Use these logical records; shared manifests and columnar files may encode many
records together:

| Record | Required information |
| --- | --- |
| Occurrence | `occurrence_id`, value codec/version, and immutable inline value or content reference. Equal-valued root occurrences remain distinct. |
| State | `state_id`, format version, and membership representation. A composed representation pins its base state and revision; a materialized representation names complete membership. |
| Membership | `member_key`, `occurrence_id`, and value reference recoverable through that occurrence. Keys are unique within a state. |
| Revision | `revision_id`, `base_state_id`, `result_state_id`, and an explicitly ordered edit sequence. |
| Membership edit | Unique sequence number, `member_key`, and `put` or `remove`. A `put` identifies the replacement occurrence. |
| Value edit | Source occurrence, ordered JSON Patch instructions, new occurrence identity and value reference, and the actual transformation execution and result bindings. |
| Checkpoint | Logical state identity, complete membership references, format version, integrity evidence, and recoverable revision/provenance references. |

A member key addresses a membership slot, not a field in the payload. States are
unordered dictionaries with no separate position vector or `move` edit. Preserve
meaningful source positions as ordinary member data. An operation that consumes
members in a particular order retains its deterministic sorting rule as material
configuration, including directions and any collation, missing-value, or tie-break
rules. Its dependencies account for the selected values and the order it consumes.
Sorting by identity cannot recover source order that was discarded. Imported
ordered data must retain the positions needed to recover its meaning.

The initial binding uses string keys. A generated key derives deterministically
from a pinned source sequence or the first occurrence it addressed. Retain the assignment;
reconstructing the same state recovers the same keys. Generated keys are
addressing metadata by default. Include them in correspondence whenever an
operation or selected-value definition makes them material, regardless of who
assigned them.

Resolve a revision at two levels:

1. **Derive changed values.** Apply a partial value edit to the named immutable
   source occurrence using ordered [JSON Patch](https://www.rfc-editor.org/info/rfc6902/)
   instructions. An `add`, `replace`, or `test` without a `value` member is an
   invalid patch; an explicit null `value` is valid. JSON Patch requires ignoring
   unrecognized members of each patch operation; this exception does not relax
   fixed Core record validation. Reject invalid patches and paths, failed
   preconditions, and results outside the declared codec. Recover the complete
   resulting value and assign a new occurrence identity. Record its actual
   generation, usage, and established derivation through the transformation
   operation. A containing batch operation may supply these relationships; no
   separate execution per field is required. Returning an existing occurrence
   preserves its provenance.
2. **Resolve membership.** Insert that occurrence at the addressed key. Across
   an explicitly ordered revision chain, the last `put` or `remove` for each key
   determines its membership; untouched keys inherit from the base. A `remove`
   requires a live key.

Evaluate edit preconditions against their preceding effective state before
reducing membership changes. A later valid edit must not hide an earlier invalid
one. Independent keys may be processed together; repeated edits to one value
retain their declared sequence. Reject conflicting edit sets without a defined
order, invalid or duplicate sequence numbers, and implicit merges of concurrent
branches. A merge must supply explicit composition semantics and provenance;
wall-clock timestamps do not establish application order.

Membership reduction must agree with
[PROV-Dictionary](https://www.w3.org/TR/prov-dictionary/). An insertion relation
accounts for all changed key/entity associations between its endpoints; a removal
relation accounts for all removals. Mixed edits use a recoverable ordered sequence
of applicable relations or general transformation provenance. Last-edit-per-key
is the resolver algorithm, not permission to emit an incomplete dictionary
relation. A value patch is a derivation of a new entity followed by its insertion
at the key, never an insertion alone.

Store base membership and edits with key/partition indexes so small edits can
read affected partitions and reuse unchanged references. Define checkpoint
thresholds by replay length and touched bytes. A checkpoint materializes the same
logical state, including full values, multiplicity, and keys; it creates no new
logical occurrence or state merely to change storage. Verify equivalence before
using it, and preserve every still-retained state's recovery path and required
provenance through compaction. Full-state access may scan all members; point
access need not replay all history.

### 3.2. Selected-value definitions and evaluation

Core calls a defined selection a **projection**; this plan says selected value to
avoid the dataframe meaning of projection. JSON Pointer supplies addressing;
DocSpec also defines the selected result, missing-value behavior, comparison, and
retention. Use a versioned definition with these fields:

| Field | Meaning |
| --- | --- |
| `kind` | `whole`, `json_fields`, or `state_members`. Whole-state dependencies remain available when the complete keyed state is relevant. |
| `selectors` | For `json_fields`, an ordered list of unique labels and JSON Pointers. For `state_members`, reuse a per-member `whole` or `json_fields` selection. |
| `scope` | For `state_members`, all members or a named set of string member keys. Reject duplicate requested keys and sort their canonical encodings by byte order, independently of caller order. |
| `comparison` | Value comparison, or comparison that additionally requires the selected entity identities. |
| `structure` | For `state_members`, whether member keys are material and an optional reference to the operation's retained sorting rule. Without a sorting rule, compare a multiset and preserve duplicate counts. |
| `missing` | A distinct absent result for a well-formed address that does not resolve. Invalid syntax or undecodable content is an error. |
| `version` and parameters | Definition-language version, value codec/version, and every parameter affecting interpretation. |

Keep the binding's parent entity, state, occurrence, and member-key origin outside
its equivalence key unless the definition makes those identities material. A
changed state identity alone must not invalidate a value-only URL selection.

Implement the following rules:

- **Whole values and composites.** `whole` selects the complete value. For JSON,
  the empty [JSON Pointer](https://www.rfc-editor.org/info/rfc6901/) selects the
  root. `json_fields` returns a labeled composite in declared selector order;
  selecting several fields cannot collapse into an unframed concatenated string.
- **Types and presence.** Return a tagged absent result or a present value in its
  declared codec. Present null, false, zero, empty string, empty array, and empty
  object remain distinct. Preserve numbers versus strings and nested types;
  never coerce different row values to a common string or lossy numeric type.
  Keep presence/type columns beside encoded values where columnar execution
  cannot represent a heterogeneous value directly. Encoding is specified in §5.
- **Array addressing.** A numeric token addresses an array index in the particular
  bound parent value; the same token addresses a property name in an object.
  Array insertion or removal can change what that index selects. Reevaluate
  against the revised parent; do not silently follow the former element. Stable
  element identity needs an explicitly defined keyed selection. The `-` append
  token used for JSON Patch is not an existing array element to read.
- **State members.** Apply the per-member selector in bulk to all or the named
  members. Preserve a missing named key as absent, distinct from a present member
  whose selected field is absent or null. Preserve membership, multiplicity, and
  keys when material. When a sorting rule is referenced, evaluate its fields and
  retain the resulting sequence; otherwise compare a multiset. Origin records
  retain the exact parent and member context even when those identities and keys
  are excluded from value comparison. Changes outside the selected scope and
  fields do not invalidate correspondence by themselves.
- **Whole states.** A whole-state comparison covers complete keyed membership
  and member values; include occurrence identities when identity comparison is
  requested. A whole-state input must remain completely retained even when its
  declared dependency selects only some fields. Binding a selected value directly
  instead establishes the narrower retention obligation under Core §5.2.
- **Retention.** Retain the definition, resulting selected value, and origin.
  The value may be stored directly or recovered exactly from a retained immutable
  parent and the versioned evaluator. Keep that recovery path available for as
  long as the selection is retained. Direct retention does not independently
  require the complete parent or state. A digest alone cannot recover the value.

Group evaluation by definition and codec. Compile selectors once, extract all
needed fields in a bounded batch, preserve types and presence, then canonically
encode and hash the selected results. Reuse already verified selected values
where their parents and definitions remain unchanged. Use `json_extract`, which
returns typed JSON, never `json_extract_string`, and read `json_type` for
presence and type. Validate JSON Pointer syntax independently before calling the
engine; permissive engine behavior must not turn invalid syntax into absence.
The execution path must pass the type, absence, pointer, and canonical-byte fixtures.

For example, a bulk operation depending on all member URLs can reuse its earlier
result after titles change. A URL change or a membership change affecting that
selection must be compared. `state_members` supplies this capability through the
same DuckDB field-evaluation path; it adds no second selection engine.

### 3.3. Engine decision and the canonical-bytes gap

DuckDB is the bulk engine. A 2026-09-13
[probe](history/probes/2026-09-13-engine-resolver-probe.py) compared last-edit-per-key
membership reduction and three JSON field extractions over two million members
and forty thousand edits on identical Parquet inputs, each engine in its own
process. The tested DuckDB path preserved number-versus-string types, computed
SHA-256 in the engine, and used less recorded memory. The tested Polars path
erased that type distinction, so the timings are not equivalent semantic work.
Equal member counts do not establish equal values or fingerprints. The probe
omitted edit preconditions, full revision semantics, and larger-than-memory
input; C03 and C25 qualify the required production paths.
The [consensus record](history/2026-09-13-core-model-consensus.md) carries the
figures and their limits.

One gap is confirmed: during extraction, DuckDB 1.5.5 rewrites the lowercase
hexadecimal escape of a control character such as U+001F to uppercase, including
inside extracted objects. The values are equal; the bytes and hashes are not.
Engine extraction therefore passes through the shared canonical encoder before
correspondence hashing. Use the shared decoder and encoder for every new or
changed JSON value, including values produced by extraction or editing; already
decoded values need no second decode. Reuse unchanged admitted canonical bytes
and verified digests within their declared integrity scope. Do not gate encoding
on the presence of a control-character escape. C03 and C05 check canonical bytes
across the admitted domain and measure the remaining Python calls, allocations,
and conversions. Arbitrary engine JSON text is never a substitute for canonical
bytes.

The metadata owner supplies bounded, consistent snapshot batches for DuckDB joins.
All authoritative writes remain in the SQLite backend.

### 3.4. Batch boundaries and cost controls

Use this bulk path through DuckDB:

```text
retained base and ordered edit batches
→ derive changed occurrence values
→ resolve membership and selected values
→ compare relevant dependencies
→ identify candidate work
→ retain content and publish result/selection batches
```

Keep queryable IDs, versions, membership keys, presence, and fingerprints columnar;
keep complete opaque payloads in their declared encoding. Use Arrow/PyArrow batches
or streams between compatible components and avoid whole-table materialization.
The SQLite adapter converts only bounded metadata batches to its parameter rows;
that conversion is an explicit measured cost, not a zero-copy Arrow interface.

| Work that can dominate | Required design |
| --- | --- |
| State reconstruction and change detection | Select relevant keys or partitions, bound replay with checkpoints, and preserve sequential dependencies while processing independent work in batches. |
| Parsing, allocation, and copying | Decode changed values and selected fields only as needed; avoid repeated decode, freeze, copy, and encode cycles. |
| Dependency and provenance processing | Load relationships in batches and traverse only the relevant graph. |
| Repeated small storage operations | Batch queries and transactions; stream large content. |
| Canonical encoding and hashing | Share parsing and encoding passes where practical and reuse verified digests only within their defined integrity scope. |

Bound rows and bytes per batch, queued work, scratch storage, and concurrent tasks.
Coordinate worker counts with engine threads so every worker does not consume all
cores independently. Python callbacks per value must appear in the measurements.
Global dependencies can legitimately require global work. Measure the rows and
bytes actually touched by a small edit.

## 4. Content storage and metadata publication

Preserve this separation:

```text
logical artifact identity → physical content reference
```

Two captures with identical bytes may share physical storage while retaining
distinct logical identities and producing executions. The existing
content-addressed [blob store](../src/docspec/adapters/storage/blobs.py) supplies
that: SHA-256 keyed, put-if-absent, streamed, verified on read, with an S3
adapter. Its object names are physical content references; DocSpec's ledger owns
logical records. Removal goes through the retention policy operation below.

Implement removal in C07/C18. The current
[retention inventory](retention-preview.md) is read-only, and the existing
[blob interface](../src/docspec/ports/blob_store.py) has no deletion operation.
Add bounded backend deletion behind the policy owner, protect shared and
in-flight references, and record recoverable intent and outcomes before and
after removal. Qualify local file and directory durability before reporting
content ready for ledger publication, including after new object names are
created. Test publication against concurrent cleanup for new and reused content.
The S3 adapter must establish the same logical guarantees through its own storage
semantics. Keeping the store does not establish these guarantees by itself.

Use **SQLite through standard-library `sqlite3`** for the authoritative metadata
ledger. One backend owns connections, parameterized SQL, statement reuse, bounded
parameter/result rows, transactions, schema initialization and version checks,
and database constraints. Use `executemany` for inserts and set-based SQL for bulk
checks and candidate lookups. No ORM or compiled ledger component. See
[Python's SQLite interface](https://docs.python.org/3/library/sqlite3.html).

Expose a small set of metadata operations instead of individual SQL calls:

| Operation | Responsibility |
| --- | --- |
| `publish_results(batch)` | Check publication conditions and atomically commit each publication unit's results, bindings, provenance, and selected-result associations. |
| `record_selections(batch)` | Retain new request/result associations for existing results, including their requested input bindings, without inventing another producing execution. |
| `find_candidates(request_batch)` | Retrieve candidate retained results for a batch of requests. |
| `read_dependencies(result_batch)` | Return the required dependency relationships for a batch of results. |
| `select_current(dataset, target, expected_current)` | Move the dataset's guarded current pointer to a retained state or result; refuse a stale expected current; rewrite nothing else. |
| `remove_under_policy(policy, batch)` | Remove retained content or records only as the cited retention policy permits, recording what was removed and why. |

These six operations do not enumerate every internal metadata write. The same
backend also records attempt start/progress/failure, dependency omissions and
supplements, and retention-policy definitions through bounded internal methods.
Give updates stable identities and defined retry semantics; keep added evidence
distinct from original execution records. Final successful-retention status and
its result bindings commit atomically through the publisher. Failure or progress
records must not imply that their incomplete inputs or outputs were retained.
The same owner admits imported entities and complete root states through the
content-readiness and atomic-publication checks, without requiring an invented
producing execution or operation result.

Define batch schemas independently of the caller's representation. Accept bounded
parameter rows or convert bounded Arrow metadata batches within this backend;
return bounded results. Keep payloads in retained content rather than converting
entire datasets to Python tuples for ledger insertion. Use temporary request
tables or equivalent set-based joins for large lookups instead of one query per
request. Measure conversion, binding, query execution, and commit together.

Centralize and batch metadata publication around SQLite's single-writer boundary.
Keep write transactions short; prepare content and bulk checks before acquiring
the publication transaction, then enforce any concurrency-sensitive conditions
inside it. Avoid per-record commits and per-source query round trips. See
[SQLite's concurrency guidance](https://www.sqlite.org/whentouse.html).

Enable foreign-key checks on every connection, use explicit transaction control,
and define a bounded busy timeout and retry policy. Use WAL with
`synchronous=FULL` for the authoritative ledger; qualify any weaker
[durability setting](https://www.sqlite.org/pragma.html#pragma_synchronous)
separately rather than comparing it as equivalent. Tag the schema version and
test initialization, reopen, and refusal of unsupported versions. Connection
ownership and shutdown must be explicit; workers submit publication batches to
the local writer.

Bulk state content can remain in retained files or manifests named by the ledger;
its metadata need not duplicate every payload row.

Publish a result in this order:

1. Retain any new immutable content required by its input and result bindings.
2. Check that the data and supporting descriptions are recoverable.
3. Publish the result and required associations in a metadata transaction.

The transaction is the visibility boundary for a successful result or retained
selection. Define publication units explicitly, keep referenced content protected
from cleanup through publication, and use the same transaction mechanism for new
results and reuse selections. Retry publication with stable identities so an
uncertain commit response can be reconciled without duplicating logical results.
Publication retry keeps those identities. Verified continuation of an unfinished
execution may keep its attempt identity; invoking the producer again after an
interruption records a new attempt and preserves the earlier one. Explicitly fresh
execution also receives a new attempt identity.

Before admitting execution relationships or publishing successful retention,
check PROV consistency and reject generation self-dependencies or cycles across
the incoming batch and retained relationships. Preserve usage/generation ordering
for streamed entities. This is distinct from ordering operation definitions or
traversing conservative dependency declarations. Recheck concurrency-sensitive
availability and evidence versions at publication so cleanup or a newly recorded
omission cannot invalidate a selection between candidate lookup and commit.

A crash before publication may leave unreferenced content; cleaning it up is one
retention policy among others, and every removal cites the policy record that
permits it (Core §5.6). A published successful result must reference retained
content. Exercise both crash points and adapter durability; an SQL transaction
alone cannot prove that external bytes survived. Computation and persistence may
still overlap under Core §5.4. Downstream computation may consume in-memory or
streamed inputs while retention completes. It need not reread storage to
establish successful retention. Separately represented operations still retain
their required bound inputs and outputs, even when execution is fused. Internal
transient values need no new artifact unless the declared operation boundaries
require one (§5.5).

## 5. Validation, encoding, and fingerprints

Use **msgspec** for fixed Core implementation records and ordinary JSON encoding
and decoding. Define requests, operation descriptions, manifests, and result
records as typed structures. A resource description marks its identity and
version as established or uncertain; the marker enters the correspondence
preimage, so an unknown version never matches an established one. Matching
uncertain descriptions alone does not establish correspondence: operation and
dependency evidence must first establish adequacy under Core §6.2. Policy may
reject an eligible result; it cannot waive a material-input or resource-evidence
gap. Reuse typed decoders and encoders; validate JSON during decoding and use
strict conversion for incoming builtin Python values. See
[msgspec usage](https://msgspec.dev/usage).

Ordinary `msgspec.Struct` constructors do not validate field types. Route public
input through explicit validation; reserve unchecked construction for internal
code whose inputs already satisfy the record rules. Reject unknown fields in fixed
records while preserving schema-permitted application payload fields. Distinguish
omitted optional fields from explicit null, apply the numeric domain below, and
generate fixed-record JSON Schemas from the same types. Check that generated
schemas and typed decoding agree on the format's accepted values. See
[msgspec structures](https://msgspec.dev/structs) and
[JSON Schema generation](https://msgspec.dev/jsonschema).

Reject duplicate object keys at raw JSON admission before a decoder collapses
them; typed validation after decoding cannot recover that ambiguity. msgspec and
the standard library both collapse duplicates, so raw admission uses the shared
Rulespec decoder, which rejects them. JSON Patch's ignored extra operation
members remain the specific RFC 6902 exception to unknown-field rejection, not
an exception to duplicate keys.

Validate the resulting values through msgspec conversion or the supplied payload
schema as appropriate; do not decode the same raw JSON again merely to run typed
validation. Preserve the duplicate-key rule across this single admission path.

Use **jsonschema-rs** when an application or plugin supplies a JSON Schema for its
payloads. Compile and reuse each schema with its declared draft, references, and
format-validation settings. Return structured validation errors through the
DocSpec API. Keep these schemas authoritative for their payloads instead of
translating them into separately maintained Python types. Validate new or changed
values at admission, then preserve that guarantee across internal operations;
recheck whenever transformations or integrity rules require it. Reusing an
unchanged reference does not waive the integrity checks required to establish
its current availability. See [jsonschema-rs](https://pypi.org/project/jsonschema-rs/).

Each boundary has one structural validator for each rule set. Fixed Core records
use msgspec; supplied payload schemas use jsonschema-rs. A record containing a
schema-governed payload can require both checks for those distinct parts.
Retention, correspondence, and provenance rules that span records remain DocSpec
checks executed in bulk. Use typed Python objects for bounded API and operation
metadata; keep large relation tables in the engine.

Use the [existing canonical JSON domain and encoding](canonical-json.md) for the
initial JSON value codec: null, booleans, exact integers within its documented
range, Unicode scalar strings, arrays, and objects with unique string keys.
Reject floats, out-of-range integers, duplicate keys, and invalid Unicode at
admission rather than rounding or stringifying them. This is the initial codec's
domain, not a Core restriction; opaque binary artifacts remain supported. An
additional codec must declare its own exact value and comparison rules. Ordinary
msgspec output is not automatically a canonical identity encoding.

Encode a selected field as `[label, "absent"]` or `[label, "present", value]`
under that codec. Thus present null is `[label, "present", null]`; the number `1`
and string `"1"` remain distinct. Encode composites as arrays of these entries in
definition order. Include entity identities in an encoding when the definition
requires them. For a whole binary artifact, value-comparison evidence names its
codec, byte length, and verified SHA-256 content digest; retain the bytes
separately. A logical artifact ID enters comparison only when its identity is
declared material.

For `state_members`, encode each selected member with an explicit membership
presence tag, its selected value, and any material key or entity identity. This
distinguishes a missing named member from an existing member with a missing field.
Without a sorting rule, sort canonical member encodings by byte order and retain
duplicates. With a sorting rule, use its resulting sequence. Retain the scope,
per-member selection, structure, and sort-rule reference in the dependency
definition used for correspondence.

Stream framed member encodings into the digest in bounded chunks; chunk boundaries
must not change the bytes. Avoid constructing a whole-state Python array or one
unbounded list/string aggregate in the engine. Selected values must remain
recoverable independently of the fingerprint. Complete keyed-state comparison
uses the same membership encoding with all members, complete values, and keys.

The correspondence preimage is a canonical structure containing a purpose tag,
encoding version, the effective operation description, and dependencies keyed
by unique binding labels. Each dependency includes its definition and parameters,
comparison mode, codec, selected value or identity evidence, and material resource
descriptions. Canonical object ordering fixes dependency-label order. Keep origin
references separate unless material. Frame values structurally; never concatenate
unframed field text. Retain known-answer bytes and digests for each format version.
Changes to accepted values, equivalence, or encoding require an explicit version;
preserve the interpretation of still-retained formats.

Keep three identities distinct:

| Identifier | Purpose |
| --- | --- |
| Content fingerprint | Compare physical content and support deduplication or integrity checks. |
| Operation/dependency fingerprint | Find candidate retained results for a request. |
| Logical artifact identity | Distinguish entities and their provenance, including equal-content observations. |

Use **SHA-256 throughout** and record the algorithm and encoding version with
digest references. Content digests hash the exact stored bytes; correspondence
digests hash the purpose-tagged structure above. One algorithm does not make
these encodings or identity meanings interchangeable. Reuse a store digest only
when it identifies the exact bytes required for that purpose. Existing format
identifiers continue to mean their declared bytes and algorithm; never silently
relabel a historical digest. Do not persist engine-internal hash functions as
permanent identifiers: DuckDB's `hash()` is a non-cryptographic hash with no
stability guarantee across versions.

## 6. Reuse and execution

Use a candidate index with multiple retained results per correspondence key:

```text
operation fingerprint + dependency fingerprint
→ zero or more candidate result IDs
→ correspondence, availability, and policy checks
→ selected result and retained association
```

The fingerprint narrows the search. The final checks establish correspondence
and permission to reuse. Retrieve candidates for batches of requests and perform
dependency, availability, and policy comparisons in set-based SQL. Python selects
the policy and coordinates the operation. Keep origin information separate from
the equivalence key, so a capture can retain its originating occurrence while
comparing only the relevant URL projection. When an omission is discovered,
record a **dependency supplement**: the original execution or result reference,
the added dependency descriptions, the reason, and when it was recorded. It never
modifies the original record; reassessed correspondence cites both (Core §6.3).

Expose ordinary Python functions through the DocSpec lifecycle:

```text
resolve inputs
→ find and check candidates
→ apply reuse policy
→ execute when needed
→ retain content and publish the selected association
```

Record request/result associations outside memoized computation, so a cache hit
cannot skip the association. A reuse selection retains the new request's bound
data as required by Core §7.3, alongside the particular selected result and its
original provenance. Fresh execution creates a distinct attempt even when an
earlier result is eligible or the new output has identical content.

Python user functions remain a supported extension point. Provide Arrow batch
inputs and outputs for bulk transformations; a function that loops over rows in
Python retains that performance cost. Schedule bounded work units and publish
their associations in batches while preserving every logical attempt and result.

Use the standard library's `graphlib.TopologicalSorter` for the operation graph's
cycle checks and ordering; it is a handful of processors. Find potentially
affected results with a recursive query over the ledger's dependency
relationships, scoped to the relevant subgraph rather than the complete
historical PROV graph. Descendants identify potentially affected operations;
DocSpec's correspondence, availability, and policy checks decide which
operations actually execute.

## 7. Job adapter

Dagster is the optional job adapter. It schedules the same lifecycle operations
and records through its own resources, owns native scheduling and job retries,
and owns no dataset meaning. Direct Python calls implement the complete local
lifecycle without it. Nothing else is planned.

## 8. Acceptance

The [implementation tasks](core-model-implementation-tasks.md) sequence the work
and carry each task's completion check. The assembled implementation is complete
when this example runs through the installed public API, every case below passes
with recorded evidence, and the capacity targets fixed in C01 are met.

```text
create root state
→ capture documents
→ derive text
→ apply a two-field revision
→ reuse unaffected results
→ retain the new result associations
→ select the revised state as current
→ close and reopen the repository
→ inspect both complete dataset states and their provenance
```

The example and behavioral checks must cover:

- Scalar and structured roots, and equal values with distinct occurrence identities.
- Partial edits producing new occurrence entities, ordered membership edits,
  conflicting unordered revisions, and absent replacement versus explicit null.
- Number versus string selections, present null versus absence, multi-field
  composites, escaped pointers, scalar roots, and array-index changes after edits.
- Bulk URL selections reused after title-only changes; material URL, membership,
  key, duplicate-count, or consumed-order changes affect the relevant comparison.
- Named missing members versus present members with missing fields, and complete
  selected-value recovery through either direct retention or retained parents.
- Meaningful source positions retained as data, deterministic sorting rules
  retained with operation definitions, and equivalent results across chunk sizes.
- Matching results across different parent state identities when only selected
  values are material.
- Metadata-only changes that reuse unaffected capture and processing results.
- Multiple results for the same dependencies, with the exact selection retained.
- Raw versus derived roles recovered from result bindings; an artifact adopted as
  raw input in one context keeps its derived provenance from another.
- An uncertain resource version that never corresponds to an established one.
- A removal that cites its retention policy, refusal of one that does not, and
  crash leftovers cleaned under such a policy.
- Local content readiness after file and directory updates, and interrupted or
  concurrent deletion that preserves shared and in-flight publication references.
- Selecting a retained state as current, refusing a stale expected current, and
  switching back to an earlier state without rewriting either.
- An explicitly fresh execution alongside an eligible prior result.
- Whole-value versus projected-input retention obligations, including the new
  request's bindings when reusing an existing result.
- Adoption of existing artifacts with original provenance; streaming or fused
  execution that preserves declared operation boundaries and retention duties.
- Failed or interrupted attempts distinguished from successful empty or null
  outputs, plus historically retained results whose data were later deleted
  under an explicit policy.
- A discovered material dependency omission and preservation of the original
  execution record and unknown historical inputs when correspondence is reassessed.
- Complete state recovery after checkpoints or physical compaction, including
  unchanged logical identities and recoverability of every still-retained state.
- The required PROV interpretation recovered from the ledger, operation
  definitions as Plans, and complete insertion/removal relationships.

Performance evidence compares equivalent work: a change counts when the same
observable results and provenance require less measured work. Measure the
production path from request to durable publication with actual ledger schemas,
transaction sizes, durability settings, and concurrent readers. The probes in
`history/probes/` are recorded diagnostics of query shapes, not capacity claims.
