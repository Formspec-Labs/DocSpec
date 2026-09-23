# Admit a producer generation by reference (D2 spike)

**Yes: DocSpec can register a spicy-regs generation's Parquet as a pinned Iceberg
table without rewriting a byte, and read it through its own `iceberg_scan` form.**
Admitting the fork-host Federal Register generation took **12.1 s at 1.09 GiB peak**
for 1,009,005 rows. The row-copied import it would replace took 16 min 49 s at
8.80 GiB for 1,007,639 rows
([reimport receipt](2026-09-14-iceberg-catalog-reimport.md)). That time covers
rulespec admission, placing the member, registering and pinning the table, and
computing a stable occurrence identity for every row. The §6 falsifier in the
consolidation path (spicy-docs `docs/research/consolidation-path-2026-09-22.md`)
therefore does not fire, and Track D continues past D2. The CTAS fallback also
works (4.4 s, 0.28 GiB), but it is not needed.

The [harness](2026-09-23-admit-by-reference-spike.py) and
[receipt](2026-09-23-admit-by-reference-spike.json) record every number below.
Each arm ran once, in its own process, so each arm's peak RSS is its own.
Software: DocSpec `38043c4`, DuckDB 1.5.5, PyIceberg 0.12.0, PyArrow 25.0.1 and
Python 3.12.9, on a 14-CPU arm64 Mac. The harness uses DocSpec's own engine
limits (one thread, 6 GiB) and the `tools/with_iceberg.py` REST fixture. Every
command ran through `uv run --frozen`.

## Fixture

Two consecutive fork-host generations, from
`/Users/mikewolfd/Work/corpora/fork-fr-generation-2026-09-23/` (see its README):

| Generation | Artifact | Member sha256 | Rows | Bytes |
| --- | --- | --- | ---: | ---: |
| current: the index read on 2026-09-23 | `sha256:731984ca…` | `47ad1212…` | 1,009,005 | 155,924,250 |
| prior: the index read on 2026-09-21 | `sha256:6c215859…` | `18afcd6e…` | 1,008,903 | 155,924,847 |

`publication.json` names each generation's `artifactDigest`. The consolidation
doc's §2 pairs `731984ca…` with the member's byte count, but it is the artifact
root's digest; the member's digest is `47ad1212…`. The "155.9 MB, sha256
`18afcd6e…`" read of 2026-09-22 is the prior generation's member. Neither
Parquet file carries Iceberg field IDs.

## What ran

```python
# Admission: rulespec_artifacts 1.1.1, the admission DocSpec already uses.
artifact = admit_artifact(LocalMemberSource(generation), expected_pin=ArtifactPin(logical_id, artifact_digest))
# Placement: hash while writing into <root>/iceberg/<table>/data/, as a download would; fsync.
# Registration: the REST fixture, through DocSpec's IcebergCatalog client.
client.create_table(name, schema=pq.read_schema(placed), location=str(table_dir))
client.load_table(name).add_files([str(placed)])   # writes schema.name-mapping.default itself
client.drop_table(name)                            # no purge: the catalog is a disposable write handle
layer = storage._pin(table, ...)                   # seal_snapshot + a DocSpec layer root
```

```sql
-- IcebergRecordStorage._relation's read form, with no projection of the encoded-record columns:
SELECT * FROM iceberg_scan('<table_dir>', version='00001-<uuid>',
                           version_name_format='%s%s.metadata.json', allow_moved_paths=true)
```

## Results

| Step, current generation, one process | Seconds |
| --- | ---: |
| rulespec admission of root, manifest and member hash | 0.067 |
| place the member (copy, sha256, fsync) | 0.132 |
| create the table and `add_files` | 0.351 |
| pin: seal the checksum tree, write the layer root | 0.068 |
| identity pass: key, canonical row digest and occurrence ID for every row, natively | 10.511 |
| write the membership layer (key to occurrence), natively | 0.696 |
| **Total** | **12.105** (peak RSS 1.09 GiB) |

| Check | Result |
| --- | --- |
| Name mapping written by `add_files` | yes: `schema.name-mapping.default` |
| Rows read through the `iceberg_scan` form | 1,009,005, with non-null counts equal to `read_parquet` on all 23 columns |
| `EXCEPT ALL`, both directions, against `read_parquet` of the member | 0 and 0 rows |
| A second reader (PyIceberg `StaticTable.scan`) | 1,009,005 rows; key columns non-null |
| The sealed checksum of the data file equals the producer's member digest | yes, for both generations |
| `verify_members`, then `available` | 0.069 s and 0.002 s |
| Whole storage root cloned elsewhere: verify, read, `EXCEPT ALL` | passes; 1,009,005 rows; 0 and 0 |
| One byte flipped in the clone | refused: "retained Iceberg file differs from its checksum" |
| DocSpec-added bytes for the table (metadata, manifests, checksums, layer root) | 17,795 |
| Membership layer (canonical `Membership` records) | 46,871,249 bytes (46.5 B/row) |
| A compact alternative (key plus 32-byte digest) | 34,842,106 bytes |
| Fallback: `CREATE TABLE` then `INSERT … SELECT * FROM read_parquet(member)` | 4.42 s; 2 files, 188,927,777 bytes; 0.28 GiB; `EXCEPT ALL` 0 and 0 |

The by-reference workspace for this generation totals about 203 MB: the
producer's 156 MB member, 18 KB of table metadata and the 47 MB membership
layer. The row-copied workspace held 1.310 GB. The by-reference total excludes
ledger bytes. There is no per-row ledger row, and the O(1) records per
generation were not published here because no API exists yet.

## Occurrence identity across generations

The occurrence ID is computed from the table, the member key and a digest of the
row's canonical bytes, all natively:

```sql
member_key    = document_number || '@' || publication_date     -- decision 0003's identity, as DocSpec's catalog states spell it
row_digest    = sha256(<canonical JSON of the row, keys sorted>)
occurrence_id = 'urn:docspec:table-occurrence:v1:' || sha256(to_json(['federal_register.parquet', member_key, 'sha256:' || row_digest]))
              -- = stable_urn("table-occurrence", [table, member_key, "sha256:" + row_digest])
```

| prior → current | Count |
| --- | ---: |
| added | 102, all published 2026-09-22 |
| changed | 2 (`comments_close_on`) |
| removed | 0 |
| unchanged, same occurrence ID | 1,008,901 |
| what D1's hash of pin and key would report | 1,009,005 |

`changes` ran in `CoreStateStorage.changes`' own form: one outer join over the
two written membership files, keeping keys whose canonical bytes differ. It took
0.40 s. A second method compared all 23 columns directly and also found exactly
2 changed rows. Neither generation has a duplicate composite key. Document
number alone repeats: 1,008,522 distinct values in 1,009,005 rows.

A Python oracle re-derived the current generation through a different reader
and encoder family: pyarrow, Rulespec's `canonical_json_bytes`, DocSpec's
`stable_urn`, `record_value(core.Membership)` and `inline_occurrence_payload`.
Over all 1,009,005 rows it agreed with the native values: 0 mismatches in
member keys, row digests, occurrence IDs, the membership bytes actually written,
`partition_bucket`'s hash, and the full occurrence entity record that existing
readers consume. Existing readers could therefore be served from a native view
over the producer's table, with no stored JSON copy.

## Finding: DuckDB's `to_json` is not canonical JSON

`to_json` escapes control characters in upper-case hex (`\u001F`). Canonical
JSON (RFC 8785, as Rulespec implements it) uses lower case (`\u001f`). Over
200,023 edge-case and random strings, `to_json` disagreed with Rulespec on
19,953. Every disagreement involved a control character; there were 0 without
one. Rewriting the escapes afterwards would be unsafe, because a literal
backslash followed by `u001F` in the source text would be corrupted.

The rule of record escapes the raw text instead. It applies a replace chain
(backslash, quote, then each control character) only to strings that contain a
control character, and uses `to_json` for the rest. That form agreed with
Rulespec on all 200,023 strings. On Federal Register it costs 11.2 s against
7.3 s for `to_json`. The two agree on every row there: 69 rows contain control
characters, but only `\n`- or `\t`-style ones, which both spell the same way.
The Federal Register oracle alone could not have caught this, because its data
lacks the offending characters.

Other spellings the rule needs, measured against Python:

- `CAST(DOUBLE AS VARCHAR)` matched `repr` on 299,864 random and edge doubles.
  `NaN` spells `nan`.
- BIGINT `to_json` matched `str` on 100,007 values.

Rulespec's canonical JSON refuses binary floats and integers outside ±2^53−1.
Those columns therefore need the string spellings named in decision 0007.

## What current DocSpec code does not yet do

- `relations`, `stream` and `verify` project `record_identity, partition_value,
  record_json`. On a producer table they fail with "Binder Error: Referenced
  column "record_identity" not found". C27's table-layer profile must read the
  table's own columns.
- `_pin` refuses any added data file over `max_member_bytes` (256 MiB). By
  reference, 5 of the 74 fork-host tables would be refused:
  `court_opinion_clusters` (3.95 GB), `fec_source_records` (1.17 GB),
  `court_opinions` (644 MB), `court_citation_map` (441 MB) and
  `court_parentheticals` (409 MB). That limit sizes DocSpec's own writes, not a
  producer's files.
- `_pin` records a stand-in `RecordSchema`. The spike called it directly because
  no public API admits a foreign table. That is C27.

## What would make this clean result wrong, and the limits

- **The baseline is not a like-for-like encoder comparison.** The 16 min 49 s
  imported DocSpec's source catalog: 7.6 GB of source-item JSON through the
  Federal Register policy, with per-record ledger rows. This path admits the
  producer's 23 already-shaped columns and runs no DocSpec policy. The falsifier
  asks whether the path is materially cheaper, and it is. The spike does not
  isolate which part of the old 16 min 49 s was re-encoding.
- **The values differ from the §6 reference.** This spike does not compare the
  generation's contract columns with the retained catalog. That comparison is
  D3's gate. The generation's `modify_date` is NULL on all 1,009,005 rows.
- **Load.** The one-minute load average ran 7 to 27 on 14 CPUs, from other
  projects' Docker work. The identity pass took 10.5 s in the admission arm and
  16.9 s in the identity arm under heavier load. Timings are descriptive, one
  repetition each.
- **No network.** Placement read a local file that was probably warm in cache.
  A real download adds transfer time but no DocSpec CPU.
- **The membership layer was written with `COPY`, not through DocSpec's writer.**
  `retain_batches` runs a Python loop per row (`_incoming`). C27 must write
  membership natively.
- **Read checks share an engine.** `EXCEPT ALL` compares two DuckDB readers of
  the same bytes. The pyarrow-based oracle is the cross-family check: its row
  digests cover every column of every row.
- **Verification memory.** The `EXCEPT ALL` arms peaked at 4.3–4.8 GiB for their
  hash tables. That is the cost of the check, not of admission.

## Reproduce

```sh
uv run --frozen python tools/with_iceberg.py \
  uv run --frozen python docs/history/probes/2026-09-23-admit-by-reference-spike.py \
  /Users/mikewolfd/Work/corpora/fork-fr-generation-2026-09-23/pins.json RECEIPT.json
```

The run takes about four minutes. Temporary storage lives under the fixture's
`TMPDIR` and is removed afterwards.
