# Use DocSpec from Python

`CoreWorkspace` is the public local runtime. It connects the SQLite metadata
ledger, immutable content, bulk state storage, and the common operation
lifecycle. Closing and reopening the workspace preserves exact result selections.

## Read existing data for another application

Open with `create=False` so a missing workspace never initializes storage:

```python
with CoreWorkspace(workspace_path, create=False) as workspace:
    with workspace.open_state("catalogue", expected_pin=accepted_pin) as reader:
        print(reader.pin, reader.record_count)
        for key, occurrence_id, value in reader.values():
            consume(key, occurrence_id, value)
```

Omit `expected_pin` on an initial inspection. The returned SHA-256 binds the state,
selected physical representation and exact layer references. Reopen with that pin
to refuse a different selection. A physical checkpoint can change the pin while
preserving the logical state. Opening requires a successfully retained state and
representation in the local publication ledger. It freshly verifies the exact
snapshot files against their pinned checksums and reuses publication's logical
row, count and membership checks, including after a process restart. Reads share
that verification until the context closes. Explicit full audits and admission
of new external data still check logical rows.

`lookup(key, occurrence_id=...)` selects one member and optionally checks its exact
occurrence. `read_value` resolves inline JSON, retained JSON or opaque bytes using
the existing codec readers and 8 MiB bound. A missing member raises `LookupError`;
JSON null remains a value. `values()` streams decoded triples without per-row lookups.

For native consumers, `relation()` exposes the existing membership/occurrence join
and `batches()` streams its canonical occurrence records in key order, in bounded
Arrow batches; only compact member addresses are sorted
([record storage](record-storage.md#who-owns-the-working-resources)).
`value_relation()` exposes `member_key`, `occurrence_id` and JSON `value` when every
value is inline. It raises `StateValueRelationUnavailable` from `docspec.errors`
for retained content, which callers can consume through `values()` instead.
It does not hide corruption under that exception. Close streams and relations
before leaving the reader. Open, use and close a reader on the same thread; its
protection and admission scope are thread-local. Reads require an existing bulk representation and never
publish data; no live Iceberg catalog is required. Native SQL generated from the
relation preserves exact snapshots and deletes, and requires the protected input
files to remain accessible for its execution.

## Create and revise keyed values

```python
from docspec.domain import core
from docspec.runtime import CoreWorkspace

with CoreWorkspace(workspace_path) as workspace:
    workspace.create("original", [
        ("notice", {"url": "https://example.org/notice", "title": "First title"}),
        ("other", {"url": "https://example.org/other", "title": "Other title"}),
    ])
    workspace.revise(core.Revision(
        format_version=1, revision_id="remove-other", base_state_id="original",
        result_state_id="revised",
        edits=(core.Remove(sequence=1, member_key="other"),),
    ))
    print(workspace.compare("original", "revised"))
```

`create` accepts a one-shot iterator of `(key, JSON value)` pairs. Keys identify
members; occurrence IDs identify immutable values. The writer consumes payloads
through bounded batches and spools only compact membership addresses. Equal
values may have different occurrence IDs. Revision edits have explicit sequence
numbers; an invalid intermediate edit refuses the revision.

`workspace.rows(state_id)` streams `(key, Entity)` pairs in deterministic key
order. Close the iterator when stopping early. `compare` computes complete
counts in DuckDB and returns a bounded sample; it distinguishes a changed
occurrence from changed value content.

A member can also be read by its occurrence ID, as `inspect("entity", id)`, a
`WholeInput` or a value edit do. It has no ledger row until a publication
references it by identity (a `WholeInput`, value edit, adopted or selected
output, or a directly selected origin) and pins it; reads and inspection pin
nothing. Until then a read searches every retained state's entity layer: each is
admitted once per session and one native query reads their distinct data files. A
pinned member reads from its row. Read whole states through `rows`, `open_state`
or `compare`, which never search by identity.

Run the [two-field example](../examples/core_values.py) with
`python -m examples.core_values /path/to/new-workspace`. It performs immutable
title and URL edits, keeps the original result for the title change, executes
again for the URL change, then closes and reopens the exact retained selection.

For independent value changes, use `prepare_value_edits` from
`docspec.application.core_edits`. Supply at most 256 `(occurrence_id, patch)` pairs
within the shared byte limits. It returns one prepared operation and the
`ValueEdit` records needed by a membership revision. Publish the operation before
applying that revision. Each replacement retains its own usage, generation and
derivation evidence; a failed patch refuses the batch. `prepare_value_edit` uses
the same implementation for one replacement.

## Append new records and replace updated records

Use `upsert` for a batch of new or changed records. Keys should be stable source
identifiers, such as `sourceItemId`. A supplied value replaces the complete value
at that key; unmentioned keys remain in the new state. Old states remain readable.

```python
with CoreWorkspace(workspace_path) as workspace:
    updated = workspace.upsert(
        "catalogue",  # the already imported base state
        [
            ("new-regulation", {"sourceItemId": "new-regulation", "title": "New rule"}),
            ("existing-regulation", {"sourceItemId": "existing-regulation", "title": "Revised rule"}),
        ],
        batch_id="publisher-release-2026-09-15",
    )
    print(updated.state_id)
```

The API returns a retained `State`. Each batch uses the existing Iceberg writer
and revision resolver, sharing the base's files. Payloads stream through a
temporary disk spool and bounded writer batches; compact edit instructions have
an 8 MiB limit. Split larger edit sets into separately identified batches, using
each returned state as the next base. Empty batches and duplicate keys are rejected.

Repeat the same `batch_id`, base, rows in the same order, and optional dataset
to recover the exact result. JSON formatting and object-key order do not matter.
A changed value, row order, base or dataset under that ID is rejected. Interrupted
publication resumes from its journal; earlier interrupted local work may run
again. Failed attempts remain recorded. A new batch ID means a new observation,
including when supplied values equal earlier values.

To maintain a current catalogue, first select the imported base once:

```python
with CoreWorkspace(workspace_path) as workspace:
    workspace.maintenance.select_current("initial-catalogue", "regulations", ("state", "catalogue"), None)
    updated = workspace.upsert("catalogue", incoming_rows, batch_id="publisher-release-2026-09-16",
                               dataset="regulations")
```

With `dataset=`, publication completes before the pointer advances. Its current
state must match the supplied base. A concurrent change raises `StaleBaseError`
and preserves any already published branch. Retrying a previously successful
batch never moves the pointer back from a newer version. An unset dataset must
be initialized using `select_current` before an upsert can advance it.

The matching CLI consumes the same `{ "key": ..., "value": ... }` JSON lines as
`state create` and prints the new state record:

```sh
docspec state upsert --workspace ./workspace --base catalogue \
  --batch publisher-release-2026-09-15 --rows updates.jsonl
```

Add `--dataset regulations` for guarded promotion. Discovery and downloading of
publisher updates remain with the source adapter; this API imports supplied data.

## Derive one keyed state in one operation

Use `derive` when the rows are computed from retained inputs and should publish
as one keyed state per batch. The caller supplies its own `OperationDefinition`
and input bindings; source states and the base bind as `StateInput`s, and
retained lookup values bind as `WholeInput`s. Retain each lookup value once
beforehand (for example as a state member) so every derive batch reuses the
same input:

```python
with CoreWorkspace(workspace_path) as workspace:
    workspace.create("lookups", [("dockets", {"EPA-HQ-2026-0001": "Clean Air"})])
    lookup_entity = dict(workspace.rows("lookups"))["dockets"]
    definition = core.OperationDefinition(
        format_version=1,
        definition_id=stable_urn("core-derive-definition", ["docspec.metadata", "lookups:2026-09"]),
        implementation_id="docspec.metadata", implementation_version="1",
        operation_kind="transformation", configuration={"lookup": "lookups:2026-09"},
    )
    prepared = workspace.derive(
        ((key, value) for key, value in prepared_rows),
        batch_id="metadata-2026-09-22",
        definition=definition,
        inputs=(core.WholeInput(label="dockets", entity_id=lookup_entity.entity_id),),
    )
```

Without a base the rows are written once into a digest-scoped rows state, and
the derived state presents those same files; there is no edit bound, so a whole
source derives in one call. With `base_state_id=` the rows and `removals=()`
become one `Revision` of `Put`/`Remove` edits that shares the base's files,
within the same 8 MiB edit bound as `upsert` (about 43,000 puts with typical
keys). That bound is checked as rows arrive, so an oversized change refuses
before the rest is prepared; split it into separately identified batches and
use each returned state as the next base. Each input and the rows state record
their usage, and the derived state records derivation edges to every input pin.

Repeat the same `batch_id`, definition, inputs, base, removals, dataset and rows
in the same order to recover the exact result. A changed value, row order,
removal, base or input under that ID is rejected. `dataset=` advances a current
pointer with the same stale-base check as `upsert` and requires a base.
`upsert` runs through `derive` and keeps its definition, request ID and
occurrence identities. Its request now binds a digest-scoped `rows` state
instead of the earlier `puts` state, so a batch recorded before 0.9.0 refuses
on retry rather than returning its earlier state.

Read what produced a state, and what changed between two states, rather than
tracking either by hand:

```python
request = workspace.generating_request(prepared.state_id)  # None for an imported state
source_id = next(item.state_id for item in request.inputs if item.label == "source")
with workspace.open_state(previous_id) as older, workspace.open_state(current_id) as newer:
    for key, occurrence_id, value in newer.changes(older):
        ...  # occurrence_id is None when the member was removed
```

`generating_request` returns the executed request (definition and every bound
input) of the successful operation that generated the state. `changes` yields
members in key order. When the newer state descends from the older one through
recorded revisions, only the edited keys are compared; otherwise one native pass
compares both memberships. Only differing members' values are read.

## Admit a producer generation by reference

A spicy-regs rollup generation already holds a table's rows in sealed Parquet.
`admit_generation` retains that member as a table-shaped state instead of
copying its rows into occurrence records
([decision 0007](decisions/0007-table-shaped-states-by-reference.md)):

```python
with CoreWorkspace(workspace_path) as workspace:
    admitted = workspace.admit_generation(generation_path, family="federal-register",
                                          table="federal_register", dataset="federal-register")
    print(admitted.state_id, admitted.report["counts"])
    with workspace.open_state(admitted.state_id) as reader, reader.table() as rows:
        rows.limit(5).fetchall()  # member_key, occurrence_id and the producer's own columns
```

`generation_path` is a local generation or publication directory, or an HTTPS
publication base (the `http` extra). The publication pointer is untrusted:
`publication.v2.json` when the publisher serves one, else `publication.json`
(only absence falls back). Rulespec admits the pinned complete family, the
table's footer must match its descriptor and, where its spicy-docs contract
types columns, give each column the contract names the contract's type (VARCHAR
unless typed), and the member is then linked into the record store unrewritten,
sealed under its producer digest. A table published split (version 2's
`partitionColumns` and `members`) is one state over all its members: each
member's key names its partition, its rows hold only that partition's values,
its footer has the declared columns, and member rows sum to the table's. Its
occurrences are those of the same rows published as one file
([history](history/2026-09-27-publication-v2/README.md)). The member key is the table's
declared spelling: identity fields the artifact declares (one field spells as
`value/1`), then the identity and `name/version` key spelling its spicy-docs
table contract declares, an entry of `KEY_SPELLINGS` whose function is the
reference DocSpec's SQL is checked against (DocSpec compiles `value/1`,
`federal-register-source-record-id/1` and `at-joined/1`, the last for any
identity of two or more VARCHAR, DATE or integer columns), then, only while the installed
spicy-docs has no Federal Register contract, `number@date`
([decision 0003](decisions/0003-federal-register-record-identity.md)). A
contract that declares no spelling, or one DocSpec does not compile, a NULL,
empty or repeated key, an
unsupported column type and a column recording when a row was observed or
fetched (`*observed_at`, `*fetched_at`, ruling R5) refuse before anything is
retained.

Each admission publishes one metadata unit, whatever the row count: the state,
its representation, and a result that binds the generation's root and member
manifest (retained as exact bytes) and generates the state and a report. An
occurrence is identified by family, logical table, member key and the digest of
its row (`docspec-table-row/1`), so an unchanged row keeps its occurrence across
generations. With `dataset=`, the dataset's current state is the base: one
native all-column comparison finds the added, removed and changed keys, only
those rows are minted, and the membership shares the base's files. The report's
`counts` give `generated` occurrences, new to the dataset's minted-occurrence
index, and `adopted` ones it already held, including a row restored to an
earlier value. A later generation must keep the dataset's key spelling; a
schema change re-mints every row and reports `reminted`. The current pointer
advances only while the admission's base is still current (`StaleBaseError`
otherwise). Re-admitting a pin returns its state and report without writing.

Every reader works on a table-shaped state: `rows`, `values`, `lookup`,
`read_value`, `changes`, `compare` and selections spell each occurrence record
from its row, natively for a whole state and by the Python reference for named
members, each checked against its occurrence, so a point read binds no native
row expression. `reader.table()` returns the typed rows without JSON.
An occurrence read by identity resolves through the index to the newest state
holding it; a publication that references one keeps its exact bytes in the
ledger. A table-shaped state is revised only by admitting the next generation:
`revise`, `upsert` and `derive` on it refuse. Under ruling R3 a dataset keeps
its current generation only: once the next generation is current and no
current result binds the superseded one, remove its state, representation and
admission result under a retention policy (below); its member file goes with
them, while files and index entries the newer state shares stay.

## Derive a typed table layer

When derived rows share one declared schema and arrive in bulk, such as a
consumer's prepared fields, `derive_table` writes them as a table-shaped state
rather than one JSON value per member
([decision 0007](decisions/0007-table-shaped-states-by-reference.md), item 9).
It is `derive` with typed rows: Arrow record batches in a `TableSchema` that
declares `member_key` and `source_occurrence_id`, the occurrence of the source
row each row derives from:

```python
schema = TableSchema("search-prepared:1", (
    ("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"), ("title", "VARCHAR"),
    ("identifiers", "VARCHAR[]"), ("date_publication_date", "DATE")))
with CoreWorkspace(workspace_path) as workspace:
    derived = workspace.derive_table(
        batches, schema=schema, batch_id="prepared-2026-09-25", definition=definition,
        inputs=(core.StateInput(label="source", state_id=source_id), lookups), dataset="prepared")
    print(derived.state_id, derived.report["counts"])
    with workspace.open_state(derived.state_id) as reader, reader.table() as rows:
        rows.limit(5).fetchall()  # member_key, occurrence_id and the declared columns, typed
```

Rows are spilled once to scratch, minted and written natively. Only an
incremental derive's membership delta, as an admission's does, passes through
Python row by row, and only up to 65,536 changes; a larger one is rewritten
natively. Each row's occurrence follows `docspec-table-row/1`,
scoped by the definition ID and the schema ID, so the same rows under another
definition are other occurrences and the row digest covers every declared
column, its source occurrence included. `VARCHAR[]` holds lists, so keywords
need no JSON. A schema with `segment_index` (INTEGER or BIGINT) is a
one-to-many layer keyed `member_key#segment_index` (`member-segment/1`); its
typed rows keep the source member's `member_key`.

With `base_state_id=`, pass the rows of the source members that changed, from
the source's `changes`, and their removed keys as `removals=`; a changed member
that now derives no rows is a removal too. Every row of a named member is
replaced, and a row whose occurrence the base already holds is not written.
The table takes the difference as one delta that shares the base's files, so
each table snapshot descends from its base's; the membership does too, up to
the admission's bound (65,536 changes or 16 files), beyond which it is
rewritten natively in one file. Rows keep the order the caller streams them in.
There is no edit bound, and the derived `changes` are at most the source's: a
row records its source occurrence, so a changed source member rewrites its row
even when the other fields are unchanged. The base must be a derived state of
the same definition and schema; derive a new definition in full, without a
base.

One unit publishes the definition, a `rows` entity (the rows' digest, the
removals and the base, bound as a whole input beside the caller's inputs), the
request, execution, result, report, state and representation, whatever the row
count. `generating_request` names the sources and lookups. The base is recorded
as used but never bound, so an older derived state can be removed while a newer
one shares its files; removal frees only the files no retained layer names.
Repeat the same batch ID with the same definition, inputs, base, removals and
rows, in any order, to get the published `TableDerivation(state_id, report)`
back without writing; a change under that ID refuses. `dataset=` advances a
current pointer from the base, or from none, with the stale-base check.

For a layer joining several inputs, declare `source_occurrence_id` as
`VARCHAR[]` holding every joined row's occurrence. `reader.affected(older,
newer)`, given two open readers of one input, yields the typed rows derived
from a member that `newer` changed or removed, so the caller re-derives only
those rows. It finds the differing members as `changes` does, from certified
revision history when `newer` descends from `older` through recorded
revisions and otherwise in one pass over both memberships (table-shaped
inputs record no revisions), then semi-joins their earlier occurrences with
the layer's lineage before reading any row. A member `newer` added names no
existing row, so a row it would newly join, such as a document whose docket
now exists, is not found: match the added members from `changes` against the
layer's own join columns.

A consumer that scans the table in place, such as a search index over
`iceberg_scan`, names it with `reader.table_reference()`: the absolute path of
the pinned metadata file and the snapshot it pins. An incremental derive's
metadata keeps its base's snapshot in its history, so such a reader can refresh
by the difference, but the base's own files, its manifest list among them, stay
only while the base state is retained. The consumer keeps the state it last read
retained until it has read a newer one. The data files are named at the location
they were written, so a relocated workspace needs DocSpec's own readers.

## Execute and reuse work

Use `workspace.operations.run(definition, request, producer)` for a fresh
attempt. Use `resolve` with a selection ID, target and reuse policy to choose a
matching retained result or execute the same producer. A producer receives an
operation context: read its declared inputs, generate or adopt outputs, and
record actual derivation relationships there.

A request declares its material dependencies. Whole inputs, named fields and
state-member fields use the same typed comparison rules. A matching hash finds
candidates; dependency adequacy, exact comparison evidence, availability and
policy determine whether a candidate can be selected. Different results may
coexist for the same comparison key.

A selection ID identifies a particular choice and its retry. Repeating it
recovers that exact choice. Supply another selection ID and `fresh=True` for
another observation. `workspace.inspect("selection", selection_id)` shows the
requested inputs, selected result and original execution request. Output
availability comes from ledger metadata; inspecting it does not audit every
output byte.

For verified continuation and publication recovery, use the same
`CoreOperations.resume` and `recover` owner. Checkpoints and interrupted
publication journals remain protected while the recorded operation is
recoverable. A failed attempt remains visible even when a later attempt succeeds.
Completed records are stored once in the ledger; new publication journals name
their keys instead of copying output values. Publication still checks and retains
those records atomically. Existing journals remain recoverable.

## Capture first and process later

```python
from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.document_processors import content_statistics_processor
from docspec.domain.content import CandidateFile, SourceItem
from docspec.runtime import CoreWorkspace

with CoreWorkspace(workspace_path) as workspace:
    documents = workspace.documents(fetcher=LocalFileContentFetcher(input_path))
    documents.import_sources([
        SourceItem("notice", "1", (CandidateFile("text", "notice.txt", "text/plain"),)),
    ], state_id="catalog")
    documents.run("catalog", run_id="captured", extract=False, segment=False)
    documents.run("catalog", run_id="processed",
                  processors=(content_statistics_processor(),))
```

The second run reuses usable captures. Extraction, segmentation and processor
graphs use Core requests, results and selections. Retained segment values keep
positions and source coordinates. Choose other extractor and segmenter objects
through `workspace.documents`; their implementation settings become material
operation definitions.

The pipeline groups candidate lookup and publication across documents at each
stage. `max_source_bytes` and `max_generated_rows` optionally limit newly fetched
bytes and generated bulk rows across attempts at the same run. Core checkpoints
preserve those counts on handled failure; reused work is uncharged, and zero
permits reuse only. Source, configuration and limits are pinned to the run ID.
An abrupt kill without a checkpoint leaves consumption uncertain, so continuation
refuses to reset it. A new run ID deliberately starts a new budget and can reuse
completed stages. The history preserves failures and successful empty results.

The [offline walkthrough](../examples/offline_demo.py) proves failure repair,
reopening, configuration and vocabulary changes, unchanged upstream work, exact
quote offsets, and agreement with a clean rebuild. The
[representation walkthrough](../examples/representation_choices.py) compares
markup with visible text while preserving source evidence.

Source catalogs remain independently usable through `build_local_catalog`,
`open_local_catalog`, and `preview_local_catalog`. These accept a workspace or a
path and use the existing source-catalog readers and policies. A catalog-only
operation does not create a Core ledger.

## Export retained evidence

`workspace.export(state_id, destination, producer=producer,
max_output_bytes=limit)` writes an independently admitted Rulespec artifact and
returns its `ArtifactPin`. The producer description must contain pinned
implementation identifiers. Open it with `docspec.result_export.open_result_export`,
supplying the expected pin, producer and byte limit. Its `rows`, `record`, and
`read_blob` methods work without the original workspace.

The default scope is the selected state and its required evidence. For a complete
document run, pass `additional_roots=documents.retained_roots(run_state_id)`.
This includes the run's exact source and stage selections; arbitrary JSON fields
are not interpreted as retention links. The artifact records its explicit roots.

## Select current and remove authorized content

`workspace.maintenance.select_current(update_id, dataset, target,
expected_current)` changes a current pointer only if its previous value still
matches. A target is a `(kind, identity)` pair. Selecting another state preserves
the earlier state and its history.

Content removal requires an explicitly retained `core.RetentionPolicy`. Its
closed description names `remove` keys and a `collect_unreferenced` boolean.
`remove_under_policy` refuses unsupported scope and outstanding retention
obligations. It records authorization, availability changes and each physical
outcome. Use `resume(update_id)` after interruption. Historical identities and
provenance remain even when authorized bytes have been removed. A policy names
states rather than their members; a member that no publication referenced by
identity stops resolving with its state.

## Use the same operations from commands

`docspec state create`, `state upsert`, `state revise`, `state rows`, `execute`, `retain`,
`inspect`, `compare`, `select`, `remove`, `resume-removal`, and `export` call the same
runtime. `docspec document import` and `document run` expose the document stages;
`docspec source-catalog` preserves independent source tooling. Each command's
`--help` lists its inputs.

The [task list](core-model-implementation-tasks.md) records the remaining caller,
conformance and capacity work. Passing a small example establishes behavior;
full-path throughput and memory qualification remain explicit acceptance gates.
