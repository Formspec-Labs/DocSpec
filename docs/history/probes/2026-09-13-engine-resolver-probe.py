"""Limited membership and scalar-extraction query comparison, 2026-09-13.

Run from the repository root:
  uv run --frozen python docs/history/probes/2026-09-13-engine-resolver-probe.py gen
  uv run --frozen python docs/history/probes/2026-09-13-engine-resolver-probe.py duckdb
  uv run --frozen --with polars python docs/history/probes/2026-09-13-engine-resolver-probe.py polars

Identical Parquet inputs: a base state of N members with canonical JSON payloads (one percent carry
meta.n as a string, the rest as a number) and E ordered edits with repeated puts and removes on hot keys
plus new keys. Each engine resolves membership (last edit per key wins, untouched keys inherit from the
base), extracts three projected fields, and fingerprints every row with SHA-256, in its own process so
peak RSS is clean. Results are recorded in docs/core-model-implementation-plan.md §3.3.

This is not the complete Core resolver/evaluator: it omits value patches, edit
preconditions, order changes, general selectors, and canonical re-encoding.
Repeated removes in this generated input can target an already absent key.
The two extraction paths do not preserve the same types, so their timings are
not equivalent semantic work; equal output counts do not prove equal outputs.
It is not a larger-than-memory or production capacity qualification.
"""
import hashlib, json, os, resource, sys, tempfile, time
D = os.path.join(tempfile.gettempdir(), "docspec-engine-probe"); os.makedirs(D, exist_ok=True)
BASE, EDITS = os.path.join(D, "base.parquet"), os.path.join(D, "edits.parquet")
N, E, HOT = 2_000_000, 40_000, 20_000

def payload(i, n_as_string=False):
    n = str(i % 977) if n_as_string else i % 977
    return json.dumps({"language": "fr" if i % 7 == 0 else "en", "meta": {"n": n, "tags": ["a", "b"]},
                       "title": f"Doc {i}", "url": f"https://example.org/d/{i}"}, separators=(",", ":"), sort_keys=True)

def rss_mb(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20   # bytes on macOS

mode = sys.argv[1]
if mode == "gen":
    import pyarrow as pa, pyarrow.parquet as pq
    pq.write_table(pa.table({"member_key": [f"k{i}" for i in range(N)], "occurrence_id": [f"o{i}" for i in range(N)],
                             "payload": [payload(i, i % 100 == 0) for i in range(N)]}), BASE)
    rows = []
    for j in range(E):
        k = j % HOT if j < E - 5000 else N + j                 # 35,000 edits over 20,000 hot keys, then 5,000 new keys
        op = "remove" if (j % 5 == 4 and k < N) else "put"
        rows.append((j, f"k{k}", op, f"o{N + j}", None if op == "remove" else payload(N + j)))
    pq.write_table(pa.table({"seq": [r[0] for r in rows], "member_key": [r[1] for r in rows], "op": [r[2] for r in rows],
                             "occurrence_id": [r[3] for r in rows], "payload": [r[4] for r in rows]}), EDITS)
    print(f"generated base={N:,} edits={E:,} (hot keys={HOT:,}) at {D}"); sys.exit()

t0 = time.perf_counter()
if mode == "duckdb":
    import duckdb
    con = duckdb.connect(); con.execute("PRAGMA threads=4")
    resolved = f"""
    WITH last AS (
        SELECT member_key, arg_max(op, seq) op, arg_max(occurrence_id, seq) occurrence_id, arg_max(payload, seq) payload
        FROM read_parquet('{EDITS}') GROUP BY member_key),
    resolved AS (
        SELECT b.member_key, b.occurrence_id, b.payload FROM read_parquet('{BASE}') b ANTI JOIN last USING (member_key)
        UNION ALL SELECT member_key, occurrence_id, payload FROM last WHERE op = 'put'),
    projected AS (
        SELECT member_key, occurrence_id, json_extract(payload, '$.url') u, json_extract(payload, '$.language') l,
               json_extract(payload, '$.meta.n') n FROM resolved)"""
    members, n_str, chk = con.execute(resolved + """
        SELECT count(*), count(*) FILTER (WHERE json_type(n) = 'VARCHAR'), sum(hash(fp))
        FROM (SELECT n, sha256('[["url","present",' || u::VARCHAR || '],["language","present",' || l::VARCHAR ||
                            '],["n","present",' || n::VARCHAR || ']]') fp FROM projected)""").fetchone()
    wall = time.perf_counter() - t0
    ext = con.execute("SELECT extension_name, installed FROM duckdb_extensions() WHERE extension_name IN ('sqlite_scanner','json')").fetchall()
    parity = con.execute("SELECT sha256('abc')").fetchone()[0] == hashlib.sha256(b"abc").hexdigest()
    typed = con.execute("""SELECT json_extract(p,'$.n')::VARCHAR FROM (VALUES ('{"n":1}'), ('{"n":"1"}')) t(p)""").fetchall()
    print(f"duckdb {duckdb.__version__}: members={members:,} n_as_string={n_str:,} wall {wall:.2f} s (resolve+extract+sha256) "
          f"peak RSS {rss_mb():.0f} MiB | typed extraction of n=1 vs n=\"1\": {[r[0] for r in typed]} | "
          f"sha256 parity with hashlib: {parity} | extensions: {ext}")

elif mode == "polars":
    import polars as pl
    base, edits = pl.scan_parquet(BASE), pl.scan_parquet(EDITS)
    last = edits.sort("seq").group_by("member_key").agg(pl.col("op").last(), pl.col("occurrence_id").last(), pl.col("payload").last())
    resolved = pl.concat([base.join(last, on="member_key", how="anti"),
                          last.filter(pl.col("op") == "put").select("member_key", "occurrence_id", "payload")])
    projected = resolved.with_columns(pl.col("payload").str.json_path_match("$.url").alias("u"),
                                      pl.col("payload").str.json_path_match("$.language").alias("l"),
                                      pl.col("payload").str.json_path_match("$.meta.n").alias("n"))
    pre = pl.concat_str([pl.lit('[["url","present","'), "u", pl.lit('"],["language","present","'), "l",
                         pl.lit('"],["n","present","'), "n", pl.lit('"]]')]).alias("pre")
    if sys.argv[2:] == ["lazy"]:
        # Engine-only memory: end in an aggregate with Polars' own (unstable, non-cryptographic) hash. Timing/memory only.
        out = projected.select(pl.len(), pre.hash().sum()).collect(engine="streaming")
        print(f"polars {pl.__version__} lazy: members={out[0,0]:,} wall {time.perf_counter()-t0:.2f} s "
              f"(resolve+extract+unstable hash, no SHA-256) peak RSS {rss_mb():.0f} MiB")
    else:
        df = projected.collect(engine="streaming"); t_resolve = time.perf_counter() - t0
        t1 = time.perf_counter()
        fp = df.select(pre).with_columns(pl.col("pre").map_elements(lambda s: hashlib.sha256(s.encode()).hexdigest(),
                                                                    return_dtype=pl.String).alias("fp"))
        chk = fp.select(pl.col("fp").hash().sum()).item(); t_hash = time.perf_counter() - t1
        typed = pl.Series(['{"n":1}', '{"n":"1"}']).str.json_path_match("$.n").to_list()
        print(f"polars {pl.__version__}: members={df.height:,} resolve+extract {t_resolve:.2f} s, sha256 via Python callback "
              f"{t_hash:.2f} s, peak RSS {rss_mb():.0f} MiB | extraction of n=1 vs n=\"1\": {typed} (type erased) | "
              f"no SHA-256 expression in the engine")
