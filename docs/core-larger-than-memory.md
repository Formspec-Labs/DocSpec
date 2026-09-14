# Qualify selected values larger than the process memory allowance

The [two-copy recipe](../tests/support/core_larger_than_memory.py) supplements the
frozen `core-bulk-v1` fixture. It does not alter that fixture or replace its full
runtime qualification. Run it after the full baseline and current storage-layout
checks finish, with the measured code fixed across all four stages.

Two copies contain 2,097,152 members and exactly 17,179,869,184 body bytes
(16 GiB), exceeding the 12 GiB process allowance. Metadata and canonical selection
framing make the complete selected stream larger than 16 GiB. Copy `c`, either
`0` or `1`, changes member key `k` to `c:k` and occurrence identity `e` to
`urn:docspec:two-copy:c:e`. The original canonical payload string stays identical.
Native DuckDB duplication supplies the new fixture; ordinary Core publication,
selection, encoding and comparison supply the measured result.

The original source is `/tmp/docspec-core-c03-full-20260913/base.parquet`, with
1,048,576 rows, 4,428,154,048 compressed bytes and SHA-256
`019402bb59cace267f58a3e6bdd8f89ca3860882b17be252f8962f7196180b23`.
The recipe verifies that pin before duplication, records the derived file digest,
and refuses changed recipe or implementation files between stages. Its receipts
pin all DocSpec Python sources, shared fixture/oracle helpers, dependencies and
recipe bytes. Keep `fixture.json`, the four measurement receipts and the exact
measured checkout or wheel together. A changed implementation needs a fresh trial.

Allow 80 GiB for the new trial and its scratch files, alongside the retained
original source. Use the reference 48 GiB host, a 6 GiB DuckDB setting and one
native thread. Full admission has the amended 20 GiB process allowance;
selected-value evaluation and independent verification retain the 12 GiB allowance.
Build has the existing 1,800-second ceiling; complete selected-value evaluation has the 600-second
ceiling. Derivation and independent verification are timed but have no latency
ceiling. These targets apply to the measured workload; broader implementation
acceptance is recorded [separately](core-model-implementation-map.md#implementation-acceptance--2026-09-14).

Run each stage in a separate process from the pinned checkout:

```sh
uv run --no-sync python -m tests.support.core_larger_than_memory derive /tmp/docspec-core-two-copy \
  --source /tmp/docspec-core-c03-full-20260913/base.parquet
uv run --no-sync python -m tests.support.core_larger_than_memory build /tmp/docspec-core-two-copy
uv run --no-sync python -m tests.support.core_larger_than_memory select /tmp/docspec-core-two-copy
uv run --no-sync python -m tests.support.core_larger_than_memory verify /tmp/docspec-core-two-copy
```

`derive` preserves the original file and writes a separate `base.parquet`.
`build` admits and durably selects the state through the shared runtime recipe.
`select` retains whole member values with material keys and computes the complete
comparison digest through Core. `verify` reopens the workspace and independently
regenerates every expected original value, checks both copy identities and every
retained selection row, rejects repeated keys through a 256 KiB bitset at full scale, proves the complete key population, and returns its evidence. It supplies no cached answers
to the timed selection stage.

Every receipt records elapsed time, native peak resident memory, dependency
versions, platform, engine settings and sampled trial/scratch storage. The storage
sample interval is 250 ms; it is not a hard peak guarantee. A fresh process does
not imply a cold operating-system cache. Source hashing is included in derive and
build timing. The external original file is pinned separately and is outside the
new trial's sampled storage total.

The [smoke tests](../tests/test_core_larger_than_memory.py) run all four stages in
fresh processes over 16 original members. They verify 32 distinct copied keys and
occurrences, unchanged payload strings, complete expected values, matching
selection evidence and refusal of an incorrect source pin. They validate the
recipe only. The current full trial built the 2,097,152-member state in 669.24 s
at 11.88 GiB peak resident memory and selected its values in 435.12 s at 8.24 GiB.
Independent verification then checked all 2,097,152 rows in 407.46 s at 9.60 GiB
peak resident memory. Its 17,523,951,228 selected bytes and SHA-256 evidence
matched the retained selection, with unchanged source and package pins. The
[acceptance record](core-model-implementation-map.md#implementation-acceptance--2026-09-14)
records this completed check. Unmet concurrency targets and unrun portions of the
expanded performance matrix remain unqualified; they do not block the accepted
implementation scope.
