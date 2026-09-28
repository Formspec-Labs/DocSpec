# Admitting spicy-regs publication version 2 (design note, 2026-09-27)

spicy-regs is moving its publication pointer to `publication.v2.json`
(spicy-regs `docs/research/multi-file-tables-2026-09-26.md`, §4.1 and §4.5
step 2; reader half at spicy-regs 6086c33, writer on `multifile-builder`).
Version 2 lists the same families as version 1. A single-file table's entry is
byte-identical to its version-1 entry. A split table's entry is
`{byteSize, rows, columns, partitionColumns, members: [{key, sha256, byteSize,
rows, partition}]}`, with no table-level sha256. Version 1 is derived from
version 2 and omits split tables. Once `bill_sections` splits, the bill
family's version-1 entry therefore no longer matches its artifact's members.
DocSpec 0.11.4 checks `set(tables) == set(members)` and so refuses the whole
family, `congress_bills` included.

This note records what changes, what must stay byte-identical, and what
refuses. Evidence is in this directory (`receipt.json`, `measure.py`, `run.sh`,
`split_documents.py`), laid out by `run.sh` under a candidate root.

## What changes

**Pointer reader** (`adapters/generation_source.py`).
- DocSpec reads `publication.v2.json` when it exists and falls back to
  `publication.json` only when the version-2 object is absent: a missing local
  file, or an HTTPS 404. The HTTPS fetcher raises a typed `HttpsNotFoundError`
  (still an `IntegrityError`) for 404. Any other failure refuses; it does not
  fall back.
- The key fixes the version: `publication.v2.json` must say `"version": 2`,
  `publication.json` must say `1`. Any other version refuses.
- A version-1 entry that lists members refuses.
- The pointer stays untrusted. Every entry is compared with the Rulespec-admitted
  artifact, as today.

**Staging.** Unchanged. `_stage` already copies every manifest member,
including nested keys, and Rulespec hashes each member once.

**Verification** (the semantic verifier). The artifact's members are grouped by
table: a key without `/` is its own table; `<table>/…` belongs to
`<table>.parquet`. `set(tables) == set(groups)` replaces
`set(tables) == set(members)`.
- A table whose descriptor has no `partitionColumns` must be exactly one member
  at its own key. It is checked exactly as today.
- A table that declares `partitionColumns` is split. Its `partitionColumns` are
  distinct names of declared columns. Every member key spells
  `<table>/<col>=<value>/…/part-NNNNNN.parquet`, in declared column order, and
  member digests are distinct. Each member's footer row count equals its
  manifest record count, and those counts sum to the descriptor's `rows`. Each
  member's footer columns (read with hive partitioning off) equal the declared
  columns, so mixed schemas refuse. Every row group's statistics for each
  partition column show no nulls and min = max = the key's value, compared as
  text; a member without those statistics has that one column scanned instead.
- A version-2 entry must equal `{**descriptor, byteSize: Σ member bytes,
  members: […]}`. Both sides' members are compared sorted by key, so listing
  order does not matter. A repeated key refuses.
- Every member's footer is checked on one DuckDB connection, rather than one
  connection per table as today.
- A member's `col=value` directory never supplies its column. Four reads open
  member paths: the staging footer, the partition scan, the registration footer
  and the admission identity pass. Only the partition scan is SQL
  (`read_parquet(?, hive_partitioning = false)` in `_check_partition`), and
  SQL's default discovers partitions: a `congress=118/` member would read as a
  BIGINT 118 in place of its own column. That `hive_partitioning = false` is the
  load-bearing guard, and the partition-value tests go red without it. The other
  three are DuckDB's Python `read_parquet`, which in DuckDB 1.5.5 does not
  discover partitions unless asked; they pass `hive_partitioning=False`
  explicitly, and `test_a_member_path_never_supplies_a_column` goes red if any of
  them turns discovery on. That test does not cover the partition scan.

**`AdmittedGeneration`** carries `members`, which are `(path, descriptor)` pairs
in key order, and `partition_columns`, in place of one `path` and `member`.

**`register_parquet`** takes one or more `(staged path, member digest)` pairs.
It is still the only registration path.
- Every footer check (declared columns, no field IDs, no row group over
  `max_member_bytes`) runs on every member before anything is placed.
- One member is placed as today, at
  `iceberg/member-<digest>/data/member.parquet`.
- Several members are placed at
  `iceberg/member-<table digest>/data/<member digest>.parquet`. Each is
  hard-linked, never rewritten, and sealed; its seal must equal its own member
  digest. One `add_files` call registers all of them in a single Iceberg
  snapshot.
- A refusal removes only what the call placed.

**`verify`** compares the table digest of the snapshot's data-file digests with
the root's `memberDigest`. The catalog's file order is not the descriptor's, so
the digests are compared sorted.

**`admit_generation`** passes every member to registration and reads all of them
as one relation for the identity pass.
- A single-file table's report keeps `"member"`.
- A split table's report names `"members"` (object key, digest, bytes, records,
  in key order) and `"partitionColumns"`.

## The split table's identity

The state ID keeps its formula: `stable_urn("generation-admission",
[logicalId, artifactDigest, family, table, rules])`. It never held a table
digest. The pin binds the whole family, and through the member manifest it
binds every member digest.

What did hold one is the registered layer: its root's `memberDigest` and its
directory. A split table has no table-level sha256, so DocSpec derives one:

> **table digest** = the member's digest when there is one member; otherwise
> `sha256` of the canonical JSON array of the member digests, sorted ascending.

- The table digest depends only on the set of member bytes, not on listing
  order, key names or partition spelling.
- A one-member split table registers exactly as the same file published whole
  would.
- A Parquet file starts with `PAR1` and the array with `[`, so a table digest
  can never equal a single file's digest.
- The layer root keeps its closed shape. `memberDigest` stays one sha256 string,
  so no root format, port type or reader changes.

## Occurrences do not depend on the split

The rules (`TableIdentity`) are the family, the logical table, the key spelling
and the columns in canonical order. An occurrence is
`[family, table, member_key, row digest]`. Neither says anything about files, so
the same rows published as one file or as several members mint the same
occurrences. A split successor over a single-file base with the same columns
takes the delta path: one all-column join, nothing re-minted, and `changes()`
reports only the rows that changed. `tests/test_core_table_states.py` tests
this directly, in both directions and with one changed partition.

The first real `bill_sections` split also adds a `congress` column, which
changes every row digest. That re-mint comes from the schema change (like B2's
`topics_json`), not from the split. In 0.12.0 it cost nothing: spicy-docs 0.46.0
declares `bill_sections`' key spelling as `at-joined/1`, which DocSpec 0.12.0 did
not compile, so `declared_spelling` (`adapters/storage/table_sql.py`) refused the
table before anything was admitted. DocSpec 0.12.1 compiles it. Before 0.46.0
spicy-docs declared no spelling for its composite identity (ruling R6);
`measure.py` supplies a provisional one for these measurements, so its numbers
stay reproducible.

## What stays byte-identical, and the proof

For a single-file table read through version 2, everything after the pointer is
the same code with the same arguments: the pin, root and manifest bytes, one
member, the columns, the key spelling, the state ID, the report, the
registration directory and the data file.
- **Tests.** Staging the same generation through a version-1 and a version-2
  pointer yields equal `AdmittedGeneration`s, apart from the temporary path.
  Admitting both into two workspaces yields equal state IDs, reports and
  occurrences.
- **Real data** (`receipt.json`). `federal-register` 497be73c (the production
  pin), `dockets`, `documents` and the bill family's `congress_bills` were each
  admitted through the live version-1 pointer. Each was admitted again through a
  version-2 pointer built with the publisher's own code (`{**parse_index(v1),
  "version": 2}`, as its bootstrap writes it). `documents` was also published
  split by `agency_code` with spicy-regs' own builder at 8d24b96 (316 members,
  2,002,888 rows) and admitted against the live single-file generation in both
  orders; `bill_sections` (2,659,863 rows) was admitted as the live-derived
  one-file table and as its 7-member split, in both orders.
- **What is compared.** `receipt.json`'s `differingFields` checks the fields
  it lists as `agreementFields`: state ID, pin, members, counts, occurrence set,
  membership rows, and the table layer's data-file digests, member digest and
  layer digest. Of those, `table.layerDigest` is the only one that differs
  between same-pin runs, but it is not the only stored field that differs.
- **Baseline.** Two version-1 admissions into two workspaces are compared the
  same way. A full comparison of everything a workspace stores for the state
  finds the same 18 paths differing between two version-1 runs as between a
  version-1 and a version-2 run of the same generation: the table, membership
  and occurrences layer references (digest, layerId, stateRef and physical
  files); the state's read pin, which hashes those references; and the
  representation's membership digest and locator. (The review's dumps,
  `~/Work/corpora/review-docspec-v2-20260927/ev`, list each layer digest twice;
  the release re-proof's,
  `~/Work/corpora/docspec-publication-v2-20260927/release-proof-9482927/dumps`,
  find the same fields as 15 paths.) Iceberg metadata carries UUIDs and commit
  times, so every layer reference belongs to its workspace. Everything else is
  equal: state ID, report, occurrences, occurrence records, membership rows and
  the data-file digest. A fresh workspace therefore reproduces production's
  state ID and occurrences, but never its read pin.
- **What a split costs** (`receipt.json` again; every step under pm01's 12 GiB
  watch). A first admission costs the same either way: `documents` 2,002,888
  rows in 15–16 s at about 1.6 GiB, `bill_sections` 2,659,863 rows in about
  230 s at about 2 GiB. A split successor over a single-file base, or the
  reverse, carries every row and generates none: `documents` in 2–3 s,
  `bill_sections` in 29 s at 7 GiB (the all-column join under DuckDB's engine
  allowance), with `changes()` alone reporting 0 in 0.7–1.0 s. `bill_sections`
  over the live table without `congress` re-mints every row — 199 s admitting,
  205 s for `changes()` alone at 8.5 GiB — the schema change's cost, not the
  split's.

## Refusals

Each refusal has a test.
- A pointer of an unknown version, or of the wrong version for its key.
- A version-1 entry listing members.
- A version-2 fetch failure other than absence.
- A member outside its table's directory.
- Two members with equal digests.
- A partition value that disagrees with the member's rows.
- A partition column that is not a declared column.
- Member rows that do not sum to the table's, or a member whose footer count
  differs from its record count.
- A member whose footer columns differ from the declared columns (mixed
  schemas).
- A member carrying field IDs, or with a row group over the bound. Nothing is
  placed in either case.
- A version-2 entry that differs from the artifact. This one equality check is
  all that refuses an entry naming a member outside the family prefix, listing
  a member twice, or whose rows do not sum; none of those has a check of its
  own.
- A registered split table whose data files are tampered with, or whose file set
  changed.
- A version-1 pointer for a family with a split table. The derived version 1
  omits the table, so the family's table set disagrees and it refuses. That is
  why version 2 must be read.
- Five of these were also staged over the real 316-member `documents` split
  (`receipt.json`'s `refusals`): the version-1-only pointer, a member outside
  the family prefix, a member listed twice, rows that do not sum, and one
  member rewritten and the artifact resealed with Rulespec so only DocSpec's
  own partition check can refuse it. Every one refused before anything was
  registered. The three pointer edits (outside the prefix, listed twice, rows
  not summing) were all refused by the version-2 entry-equality check
  (`publication table descriptor differs from its pinned member`): they exercise
  that one check three times, not three checks.

## How it scales

`m` = members in the family, `n` = rows of the selected table.

- **Requests and staging:** O(m) GETs and O(family bytes) transfer and hashing,
  as today.
- **Footer, schema and partition checks:** O(m + row groups), metadata only.
  The partition check scans one column only when a writer omitted statistics.
- **Registration:** O(members of the table) links and seals. Each seal hashes
  its file once, as a single-file registration does. One `add_files` and one
  snapshot per table.
- **Identity pass:** O(n) over one relation of all members, the same pass a
  single file of the same rows takes. Nothing is rewritten and no second pass is
  added.
- **Verify:** O(data files) digests.

Not done here, and not needed for correctness: design §4.3's per-partition
admission saving. An unchanged member's rows are still compared in the one
all-column join.
