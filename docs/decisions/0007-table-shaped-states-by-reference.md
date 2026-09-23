# Decision 0007: a table-shaped state may reference a producer's sealed Parquet, with occurrence identity from key and row content

- Date: 2026-09-23
- Status: **proposed, not accepted.** The owner must rule on this record: it is
  ruling 1 of the consolidation path (spicy-docs
  `docs/research/consolidation-path-2026-09-22.md`, §5). Nothing here is
  implemented. [C27](../core-model-implementation-tasks.md#c27--admit-a-producer-generation-by-reference)
  and [C29](../core-model-implementation-tasks.md#c29--typed-derived-layers)
  are the proposed tasks.
- Evidence:
  - the [D2 spike](../history/probes/2026-09-23-admit-by-reference-spike.md);
  - the [2026-09-14 reimport](../history/probes/2026-09-14-iceberg-catalog-reimport.md);
  - the PM01 gate, finding 1 (`~/Work/corpora/pm01-gate-2026-09-23/README.md`).
- Amends: the consolidation path's D1, which proposed occurrence IDs made by
  hashing the pin and the key.

## The proposed ruling

1. **A keyed root state may be table-shaped.** Its members are the rows of a
   pinned Iceberg table whose data files are a producer's admitted member bytes.
   Those bytes are downloaded once, stored in DocSpec's record store and sealed
   like every other layer. No row is re-encoded, and no per-row ledger record is
   written. A generation is admitted through the Rulespec admission DocSpec
   already uses (`rulespec_artifacts.admit_artifact` with the index's pin), and
   the index itself is only an untrusted pointer.
2. **One metadata unit per admitted generation.** The unit holds the state, its
   representation and the admission's operation records. The representation's
   manifest names a table layer, a membership layer in the existing
   `core-membership:1` format, and the rules below.
3. **The pin identifies the state; key and content identify an occurrence.**
   - **State:** `stable_urn("generation-admission", [logicalId, artifactDigest,
     table, rule versions])`.
   - **Occurrence:** `stable_urn("table-occurrence", [table, member_key,
     "sha256:" + row_digest])`.
   - **Consequence:** an unchanged row keeps its occurrence across generations,
     so `changes` follows real change and an incremental update stays
     incremental.
4. **Row digest rule `docspec-table-row/1`.** The digest is the SHA-256 of the
   row's canonical JSON (RFC 8785, as Rulespec implements it): an object of
   every column, keys sorted.

   | Column type | Spelling |
   | --- | --- |
   | NULL | `null` |
   | VARCHAR | a canonical string |
   | BIGINT | an integer when \|v\| ≤ 2^53−1, else its decimal digits as a string |
   | DOUBLE | a string of its shortest round-trip decimal: Python `repr`, so `nan`, `inf`, `-inf`, `-0.0` |
   | DATE, TIMESTAMP (C29) | ISO 8601 strings, TIMESTAMP in UTC |
   | LIST<VARCHAR> (C29) | a canonical array |
   | anything else | refused |

   DocSpec computes the digest natively. Its spellings must equal the Python
   oracle on a fixed edge-case corpus, and a failure refuses before any
   identity is minted. The rule's version is the occurrence URN's version
   (`urn:docspec:table-occurrence:v1:…`) and part of the state identity, so
   changing a spelling opens a new identity space and never silently re-mints
   the old one. If a
   producer ever declares a row digest, DocSpec uses it only when its
   definition is this rule.
5. **Identity source order for the member key.**
   1. Identity fields declared in the artifact.
   2. `spicy_docs.schemas.TABLE_CONTRACTS`.
   3. [Decision 0003](0003-federal-register-record-identity.md) for Federal
      Register, spelled `number@date` as DocSpec's catalog states spell it.
   4. Otherwise refuse.

   A NULL or empty component refuses, and so does a duplicate key.
6. **Derived bulk layers are typed tables too (C29).** Search's prepared
   metadata, body and segment extraction, and fusion joins write typed layers.
   Each is keyed by member key and carries its source occurrence. They use the
   same identity rules and a single metadata unit, with lineage through the
   existing operation records. None becomes a JSON value inside a JSON record.
7. **The JSON entity path remains the default** for everything not table-shaped:
   documents and captured artifacts, evidence, supplied records, and small,
   nested or irregular values. It also holds per-occurrence results and reuse
   associations.

## Why

The row copy is the measured cost. Importing DocSpec's own Federal Register
catalog took 16 min 49 s at 8.80 GiB and left a 1.310 GB workspace. Admitting
the producer's generation by reference took 12.1 s at 1.09 GiB, and its
workspace is about 203 MB, most of it the producer's 156 MB member.

On two consecutive fork-host generations, the identity in ruling 3 kept
1,008,901 of 1,009,005 occurrences; `changes` reported 102 added and 2 changed,
matching a direct column comparison. D1's pin-keyed identity would have reported
all 1,009,005. The PM01 gate saw the same effect as 10,000 of 10,000 members
changed on re-admission, which would turn every Search and Engine update into a
full derive and republish.

A Python oracle, using a different reader and encoder, agreed with the native
key, digest, occurrence ID, written membership bytes and full occurrence record
on every row.

## How it meets the Core spec

- **§3.1: occurrence identity is distinguishable from value equivalence.** The
  table and the member key are part of the identity, so equal rows at different
  keys, or in different tables, stay distinct occurrences. That also meets
  §9.2's "Equal values at different keys MUST NOT be collapsed". The same key
  with the same value in two generations is the same occurrence. §3.1 allows
  this: a key "MAY remain stable across revisions". It is how revisions already
  share unchanged occurrences.
- **§3.1: "A changed value MUST NOT be assigned to an earlier retained
  occurrence entity".** A changed value changes the digest, so it gets a new
  identity, barring a SHA-256 collision.
- **§3.2 and §3.3: complete retained state.** The retained table and the
  membership layer establish completeness. §3.2 permits "a retained manifest,
  table … or another representation". Each generation is its own state, so
  §9.2's earlier association stays recoverable while the earlier state is
  retained. No new generation overwrites an older one (§5.6).
- **§4.2 and §4.4: generation versus adoption.** Admission is a capture by
  adoption of an existing retained artifact. An occurrence that already appears
  in the dataset's previous generation is adopted, not generated again. The
  admission result records generated and adopted counts at the batch boundary
  (§4.5), and membership anti-joins recover which occurrences they were (§8).
- **§5.1: retained.** The bytes are local, immutable and sealed by the same
  checksum tree as every layer. The seal's digest for the data file equals the
  producer's member digest. A relocated copy verifies, and a flipped byte is
  refused.
- **§9.3: insertion and removal.** DocSpec asserts no insertion or removal
  relation between independently admitted generations. `changes` is computed
  from complete memberships, so it cannot omit a change between the same
  endpoints.

One consequence is named deliberately. If a key goes from value A to B and back
to A, the third generation's member is the first generation's occurrence again.
Its original generation provenance stands, and the later admission records an
adoption. That reuse is correct for reuse decisions (§7), because the value is
identical. The first admission after the row-copied catalogs is a full change
regardless, because the values have a different shape. Ruling 4 therefore costs
one full Engine republish if PM01 waits for C27, and two if it cuts over first.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| Keep the row copy | It is 83 times slower, uses 8 times the memory and holds 6.5 times the workspace, and it re-mints identity per batch. |
| D1's occurrence identity from pin and key | Every generation reports every member changed (PM01 finding 1; 1,009,005 in the spike). |
| `CREATE TABLE AS SELECT` into a DocSpec table | It works (4.4 s, 0.28 GiB) but keeps a second, 21% larger copy of bytes that name mapping reads in place. It remains the fallback if a producer file ever cannot be registered. |
| Read spicy-regs' published URL directly | The consolidation path's §7 excludes a remote profile. The producer can expire or replace objects, and Search decision 0008 forbids reading it without a copy in the workspace. |
| Identity from row content alone, without the key | It violates §9.2: equal rows at different keys would collapse. |
| Identity from DuckDB's `to_json` | It is not canonical for control characters (upper-case `\u001F`; 19,953 of 200,023 test strings). The FR data could not show this. |
| No materialized membership: recompute identity at each `changes` | It saves 46.5 B/row at about 10 s per million rows per comparison. Kept as the option for narrow, high-row tables. |
| A compact key-plus-digest index instead of `Membership` records | It is only 26% smaller, and it would need new comparison code. |

## Costs and risks

- **A producer schema change touches every row.** Adding, removing or renaming a
  column changes every row digest, so every member reads as changed. The values
  did change. A consumer whose declared dependency is a projection (Core §6) can
  still reuse its results; that reuse is later work.
- **Membership costs about 46.5 B/row.** That is 30% of the FR member, and more
  than the table itself for narrow tables. Admit only the tables a consumer
  reads.
- **Five of the 74 fork-host tables exceed `max_member_bytes`.** The table
  profile exempts referenced files and bounds row groups instead.
- **Field IDs.** `add_files` refuses Parquet that already carries Iceberg field
  IDs. If a producer starts writing them (consolidation C3), registration must
  check that they match the declared schema instead of adding a name mapping.
- **Spelling depends on the DuckDB version.** A DuckDB upgrade that changes a
  spelling fails the oracle test before it can re-mint identities.

## What would reverse it

C27's gate failing: admission not materially below the row copy, `changes`
reporting more than a direct comparison, or contract columns disagreeing with
the retained catalog beyond listed, adjudicated keys. Or a Core reading, ruled by
the owner, that adopting an occurrence across independently admitted states
breaks §3.1 or §4.4.
