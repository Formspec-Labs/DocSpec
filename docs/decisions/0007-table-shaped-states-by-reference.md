# Decision 0007: a table-shaped state may reference a producer's sealed Parquet, with occurrence identity from key and row content

- Date: 2026-09-23, revised the same day after a cross-stack review.
- Status: **proposed, not accepted.** The owner must rule on this record: it is
  ruling 1 of the consolidation path (spicy-docs
  `docs/research/consolidation-path-2026-09-22.md`, §5), plus open rulings
  R1–R6 below. Nothing here is implemented.
  [C27](../core-model-implementation-tasks.md#c27--admit-a-producer-generation-by-reference)
  and [C29](../core-model-implementation-tasks.md#c29--typed-derived-layers)
  are the proposed tasks.
- Evidence:
  - the [D2 spike](../history/probes/2026-09-23-admit-by-reference-spike.md),
    with its review corrections;
  - the [2026-09-14 reimport](../history/probes/2026-09-14-iceberg-catalog-reimport.md);
  - the PM01 gate, finding 1 (`~/Work/corpora/pm01-gate-2026-09-23/README.md`).
- Amends: the consolidation path's D1, which proposed occurrence IDs made by
  hashing the pin and the key.

## The proposed ruling

1. **A keyed root state may be table-shaped.** Its members are the rows of a
   pinned Iceberg table whose data files are a producer's admitted member
   bytes, stored in DocSpec's record store and sealed like every other layer.
   No row is re-encoded, and no per-row ledger record is written. Admission uses
   the Rulespec admission DocSpec already uses; the publication index is only an
   untrusted pointer.
2. **One metadata unit per admitted generation.** The unit holds the state, its
   representation and the admission's operation records. The version-3 manifest
   names:
   - the table layer;
   - a membership layer in the existing `core-membership:1` format;
   - the dataset's minted-occurrence index snapshot;
   - the rules.
3. **The pin identifies the state; key and content identify an occurrence.**
   - **State:** `stable_urn("generation-admission", [logicalId, artifactDigest,
     family, table_name, rule and spelling versions])`.
   - **Occurrence:** `stable_urn("table-occurrence", [family, table_name,
     member_key, "sha256:" + row_digest])`. `table_name` is the logical table
     name, not the file name.
   - **Consequence:** an unchanged row keeps its occurrence, so `changes`
     follows real change.
4. **Mint once, carry forward.** A dataset's first admission mints every
   occurrence in one native pass. Each later generation with the same schema is
   compared with the current one by a direct all-column join. Only added and
   changed rows get a digest and an ID. Unchanged keys keep their minted
   occurrence, and the membership is a delta that shares the base's files.
   Materializing membership freezes minted IDs against later spelling changes;
   that, not speed, is why it is kept.
5. **Row digest rule `docspec-table-row/1`.** The digest is the SHA-256 of the
   row's canonical JSON (RFC 8785, as Rulespec implements it), keys sorted.
   Every string, keys and framing included, uses the guarded spelling: a
   raw-text escape chain for strings holding a control character, `to_json`
   otherwise.

   | Type | Spelling |
   | --- | --- |
   | BOOLEAN | `true` or `false` |
   | INTEGER, SMALLINT | an integer |
   | BIGINT | an integer when \|v\| ≤ 2^53−1, else a decimal string |
   | DOUBLE | a string: `nan` for every NaN, else DuckDB's shortest round-trip cast, which equals Python `repr` |
   | DATE, TIMESTAMP | ISO 8601 strings, TIMESTAMP in UTC |
   | LIST<VARCHAR> (C29) | a canonical array |
   | anything else | refused |

   Native spellings must equal the Python reference on a fixed corpus before
   any identity is minted. The rule's version is the URN's version, so a new
   rule opens a new identity space and never silently re-mints an old one.
6. **The member-key spelling is a per-table function owned by spicy-docs.**
   It is declared and versioned, with a Python reference such as
   `federal_register_source_record_id` (`number@date`). DocSpec compiles the
   declaration to SQL and tests it against the reference. The version enters
   the state identity, and a dataset's later generations must keep it. A new
   spelling is an explicit re-key.

   Identity sources, in order:
   1. fields declared in the artifact;
   2. `spicy_docs.schemas.TABLE_CONTRACTS`;
   3. [decision 0003](0003-federal-register-record-identity.md) for FR;
   4. otherwise refuse.
7. **A minted-occurrence index per dataset.** It is append-only, typed and
   sorted by occurrence hash within each appended file. It records each
   occurrence's key, row digest and first admitting state. It serves R1,
   resolves an occurrence to its key without a scan, and outlives the removal
   of the state that first admitted an occurrence.
8. **Superseded generations can be removed.** Before removal:
   - member pins move to the newest retained layer holding the same
     occurrence;
   - still-referenced occurrences held by no newer layer are copied into a
     small retired-occurrence layer;
   - consumers resolve references through the newest served state.

   Historical provenance stays (Core §5.3).
9. **Derived bulk layers are typed tables too (C29).** They use the same rules
   and a single metadata unit, with lineage through the existing operation
   records. Consumers such as Engine keep their own table contracts and read
   the typed columns natively instead of parsing JSON.
10. **The JSON entity path remains the default** for everything not
    table-shaped: documents and captured artifacts, evidence, supplied
    records, small, nested or irregular values, per-occurrence results and
    reuse associations.

## Rulings (owner, 2026-09-24)

Each was put to the owner with the options below; the decision is recorded
under its item. The design stays proposed until C27 is implemented.

- **R1: provenance of a reappearing occurrence.** If a key goes A → B → A, the
  third generation's member is the first generation's occurrence. Comparing
  only with the previous generation would record it as generated twice. PROV
  allows an entity one generation, and Core §5.3 keeps provenance after
  deletion.
  - **Option (a):** admission asserts only the state's generation. Occurrences
    are members of an imported root, whose construction provenance need not be
    invented (§3.3). This is simplest, and no occurrence is ever "generated".
  - **Option (b):** the append-only minted-occurrence index records the first
    admitting state of every occurrence. A later admission generates only
    occurrences absent from the index and adopts the rest (§4.4). The index
    also serves C28's lookups (C27 step 9).
  - **Recommended: (b).** It is the only option that makes "generated once"
    checkable, and the index is needed for lookups anyway.
  - **Decided: (b),** the append-only first-admission index.
- **R2: sequencing (extends consolidation ruling 4).** Each of these forces a
  full derive and a full Engine republish of the affected source:
  - the first admission, because its values differ in shape from the row-copied
    states;
  - B2 adding `topics_json` to the FR generation, which changes every FR row
    digest and re-mints every FR occurrence;
  - C29's cutover, whose new derived occurrence IDs change every Engine
    `content_sha256`.

  D6 is a prerequisite: Search's `prepare` refuses generation rows today.
  Landing B2, then D6, then C27 with C29, then PM01's cutover costs one full
  republish. Each item that lands after the cutover adds one.
  - **Decided:** PM01's cutover waits for B2 and then C27 with C29, so the
    8091 index is replaced once, by a candidate built from admitted
    generations. The row-copied catalogs are retired at that rebuild rather
    than trimmed of their per-member rows now.
- **R3: retention of superseded generations.** Proposed: keep the current and
  previous generation of each dataset, plus any generation a current result
  binds, and remove the rest under C18.
  - **Needs:** Engine to resolve `retained_ref` through its newest served state
    (a spicyengine change), and the pin transfer and retired-occurrence layer
    in C27 step 11.
  - **Without it:** each FR generation keeps 156 MB forever.
  - **Decided: keep the current generation only.** A superseded generation is
    removed under C18 once the next one is admitted and every current result
    that bound it has been re-derived. Consequences the implementation must
    honour: the membership delta of C27 step 7 must carry what `changes`
    needs without the previous generation's files; there is no rollback to
    the prior generation inside DocSpec (the producer's sealed artifact
    remains the recovery source); Engine resolves `retained_ref` through its
    newest served state.
- **R4: narrow, high-row tables.** A first admission pays membership at about
  46.5 B/row plus the index at about 35–50 B/row. For `court_citation_map`
  (77.5 M rows in 441 MB) that is about 6–7 GB, roughly 15 times the table
  itself. The options:
  - pay it;
  - let the first generation's membership be a view over the index, paying one
    per-row cost;
  - admit such tables only when a consumer needs them (recommended).
  - **Decided:** only when a consumer needs one; the identity pass gets a
    streaming sorted writer before the first such admission.
- **R5: tables whose rows carry observed- or fetched-time columns.** 17 tables
  match by name; whether they refresh on unchanged rows was not checked. If
  they do, every generation re-mints every row. The options:
  - admit them with full churn;
  - have spicy-regs carry unchanged rows' prior values;
  - admit a declared projection without the volatile columns. A state then
    claims, and retains, only that projection (Core §3.2).
  - **Decided:** the declared projection. Identity follows the meaningful
    columns; the timestamps stay readable in the producer's file.
- **R6: composite member keys.** 31 contracts have composite identities.
  - **Recommended:** wait for spicy-docs to declare their spellings, so no key
    flips later.
  - **Alternative:** DocSpec spells them provisionally as a canonical JSON
    array, pinned for each dataset at first admission. A later spicy-docs
    spelling then forces an explicit re-key.
  - **Decided:** wait for spicy-docs. Those tables are not admitted until a
    versioned key function exists for them (consolidation B19/B26).

## Why

The row copy is expensive, but the measured baseline is not like for like.
Importing DocSpec's own FR catalog, which is different content (7.6 GB of
catalog-item JSON through DocSpec's policy), took 16 min 49 s at 8.80 GiB and
left a 1.310 GB workspace. Admitting the producer's 23-column generation by
reference took 12.1 s at 1.09 GiB, with about 203 MB in the workspace.

Most of that difference is native vectorized encoding rather than the avoided
copy. A CTAS copy took 4.4 s against 0.55 s for registration by reference, and
the identity pass is common to both. The like-for-like path, C26's JSON derive
over the same 23 columns, would take about 9 min at 0.54 ms/record (estimated,
not measured).

On two consecutive fork-host generations the proposed identity kept 1,008,901
of 1,009,005 occurrences, and `changes` reported 102 added and 2 changed,
matching a direct column comparison. A pin-keyed identity reports every row by
construction: 1,009,005, a sum rather than a measurement. The PM01 gate
measured the same effect: 10,000 of 10,000 members changed on re-admission.

A Python oracle, using a different reader and encoder, agreed with the native
key, digest, occurrence ID, membership bytes and occurrence record on every
row.

## How it meets the Core spec

- **§3.1: identity is distinguishable from value equivalence.** The family,
  table and key are part of the identity, so equal rows at different keys or in
  different tables stay distinct. That meets §9.2's "Equal values at different
  keys MUST NOT be collapsed". The same key with the same value in two
  generations is one occurrence, which §3.1 allows ("MAY remain stable across
  revisions").
- **§3.1: "A changed value MUST NOT be assigned to an earlier retained
  occurrence entity".** A changed value is found by the direct comparison and
  gets a new digest and identity. The index's digest check refuses any lookup
  whose row does not match.
- **§3.2 and §3.3: complete retained state.** The retained table and the
  membership establish completeness. Each generation is its own state, so
  §9.2's earlier association stays recoverable while that state is retained,
  and no generation overwrites another (§5.6).
- **§4.2, §4.4 and §5.3: generation versus adoption.** This is R1. Under (b),
  each occurrence is generated once, by its first admission, and adopted
  afterwards. Under (a), no occurrence generation is asserted.
- **§5.1: retained.** The bytes are local, immutable and sealed. The seal's
  digest for the data file equals the producer's member digest. A relocated
  copy verifies, and a flipped byte is refused.
- **§9.3: insertion and removal.** DocSpec asserts no insertion or removal
  relation between generations. `changes` is computed from complete
  memberships.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| Keep the row copy | Measured at 16 min 49 s and 8.80 GiB on different content, with a JSON derive over the same columns estimated at about 9 min. It also re-mints identity per batch. |
| D1's occurrence identity from pin and key | Every generation reports every member changed (PM01 finding 1). |
| `CREATE TABLE AS SELECT` into a DocSpec table | It works, and its extra 3.9 s (4.4 s against 0.55 s for registration) is small beside the shared identity pass. But it keeps a second copy, 21% larger, of bytes that name mapping reads in place. It remains the fallback if a file cannot be registered. |
| Read spicy-regs' published URL directly | The consolidation path's §7 excludes a remote profile, the producer can replace objects, and Search decision 0008 forbids it without a workspace copy. |
| Identity from row content alone | It violates §9.2. |
| Identity scoped by the member's file name | A file name is physical; this record scopes by family and logical table name. |
| DuckDB's `to_json` as the canonical spelling | It writes `\u001F` where canonical JSON writes `\u001f` (19,953 of 200,023 test strings). |
| Recompute every generation's identity from its rows | 10.4–18.0 µs per row against 2.53 µs per row for a direct comparison, and it silently re-mints on a spelling change. |
| A member-key spelling chosen by DocSpec | It would flip when spicy-docs adds a contract (B2, B19, B26), changing every Engine ID. |

## Costs and risks

- **Schema changes touch every row.** A producer schema change (B2's
  `topics_json`, for one) re-mints every occurrence of the table (R2).
- **Coverage is narrow today.** Only FR and 8 single-column contracts are
  admissible now: about 1.44 M of 141.3 M fork-host rows. 34 tables, holding
  139.0 M rows, have no identity source until B26.
- **File size.** Five of the 74 fork-host tables exceed `max_member_bytes`.
  Referenced files are exempt, and row groups are bounded instead.
- **Field IDs.** `add_files` refuses Parquet that carries Iceberg field IDs. If
  a producer adds them, registration must check them against the schema.
- **Spelling depends on the DuckDB version.** An upgrade that changes a
  spelling fails the oracle test before minting. Carried-forward IDs are
  unaffected.
- **The C28 extension point must land in the C28 lane** (C27 step 9).

## What would reverse it

C27's gate failing:
- admission not materially below the like-for-like derive;
- `changes` reporting more than a direct comparison;
- contract columns disagreeing with the retained catalog beyond listed,
  adjudicated keys.

Or the owner reading adoption across independently admitted states as
contrary to Core §3.1 or §4.4 under both R1 options.

## Corrections for the consolidation path's owner

These concern spicy-docs `docs/research/consolidation-path-2026-09-22.md`,
which is not edited here.

- D4 is labelled "DocSpec C28", which collides with DocSpec's C28 (register
  members per layer). D4 needs another ID.
- The table row at §2 line 38 pairs the artifact root's digest `731984ca…` with
  the member's byte count. The member's digest is `47ad1212…`. The "sha256
  `18afcd6e…`" of 2026-09-22 is the prior generation's member.
- §5 ruling 1, "decision 0007 as stated in D1", refers to the pin-keyed identity
  this record replaces. D1 and D3's text should follow this record once ruled.
- Any citation of "D1 would report 1,009,005" should label it a sum
  (added + removed + changed + unchanged), not a measurement.
- Ruling 4 should carry R2's full list of republish causes.
