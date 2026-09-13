# Build a catalog from retained FEC committee metadata

This example turns a pinned Federal Election Commission (FEC) committee census
into an ordinary DocSpec catalog. It keeps each committee's native fields and
source evidence so you can inspect the dataset without fetching document bodies.
SpicyDocs owns reading the retained source; DocSpec owns the catalog.

## Try the offline example

From this checkout, choose a new absolute output directory:

```sh
uv run --frozen --no-dev --extra spicy-docs python -m examples.fec_committees \
  --output /tmp/my-fec-catalog
```

The [authored fixture](../examples/fec_fixtures/README.md) contains two synthetic
committees. To exercise an explicitly empty observation:

```sh
uv run --frozen --no-dev --extra spicy-docs python -m examples.fec_committees \
  --empty --output /tmp/my-empty-fec-catalog
```

Both commands use DocSpec core and the optional SpicyDocs base package. The
example makes no network requests and needs no acquisition extra, SpicyRegs,
or Dagster. Package installation may need network access before the first run.

## Use an existing retained source

Supply all five source inputs from its handoff: the release directory, its blob
directory, both artifact pin fields, and the source verifier implementation you
accept. Use the verifier that produced that release, which may differ from the
currently installed reader's revision.

```sh
uv run --frozen --no-dev --extra spicy-docs python -m examples.fec_committees \
  --output /absolute/new-fec-catalog \
  --source-root /absolute/retained/fec-source \
  --blob-root /absolute/retained/fec-blobs \
  --logical-id "$FEC_LOGICAL_ID" \
  --artifact-digest "$FEC_ARTIFACT_DIGEST" \
  --source-implementation-id "$FEC_SOURCE_VERIFIER"
```

Set the three variables to the exact handoff values. `--empty` applies only to
the authored fixture. No live API mode or source-refresh step is hidden in this
command.

## Inspect what was retained

`dataset/sourceCatalog/` contains the ordinary catalog. `fec-example-summary.json`
contains its reference and accepted catalog producer, item/disposition counts,
source locations and pins, effective bounds, and the complete original
`collectionOutcome`. Use the [public catalog reader](catalog-evidence.md) to
stream catalog rows. The offline example also creates `source/` and
`source-blobs/` through the provider's normal publication API.

Each catalog row preserves the full wrapped provider record and selected
record evidence under `sourceNativeFacts[0].fields.metadata`. Unknown nested
fields, nulls, empty values, capture facts, and source pointers remain available.
Only the committee name is copied to the normalized title. Compact source pins
and outcome counts accompany each row; the complete request inventory, policy,
and warnings appear once in the summary.

`sourceIssuedVersion` reuses the existing `evidenceBlobRef`. This identifies the
retained response observation, **not an FEC-issued committee or document
revision**. Committees from one response can share that observation version;
their committee IDs distinguish their records. `versionMeaning` records this
interpretation explicitly. No new per-record hashing pass is added.

Keep the original source release and its blob store available. The catalog
retains evidence references; it does not copy the original response bytes into
a second store. Retain the summary with the catalog, including for an empty
observation, where no catalog row can carry source facts.

## Understand scope and limits

The source reader admits the exact pinned release with explicit verifier
acceptance. Admission is **not a fresh full replay of FEC collection semantics**.
This example then joins the ordered record and evidence streams once, checks
matching membership and published count, and refuses rejected source records or
supplied document renditions that it would otherwise discard.

Every metadata row has no document candidates and is `unavailable` for body
processing. Preserved asset links are source facts; they do not establish fetched
documents. This example creates no processing run or schedule.

The source covers only its admitted requests and filters. `observed-crawl`
does not establish a frozen publisher snapshot, all historical committees, or
publisher-wide absence when empty.

| Option | Default | What it bounds |
| --- | --- | --- |
| `--max-records` | 50,000 | Supplied committee records. |
| `--max-bytes` | 256 MiB | Canonical supplied-record bytes retained in memory; additional Python object memory is separate. |
| `--max-scratch-bytes` | 2 GiB | Catalog build database and journal space; published catalog and source blobs are separate. |

The [example](../examples/fec_committees.py) uses the existing
`SuppliedRecordSource`, `SuppliedRecordCatalogPolicy`, `build_local_catalog`, and
`open_local_catalog` interfaces. Its [focused tests](../tests/test_fec_committees_example.py)
cover preservation, empty input, refusal, bounds, and existing-source reuse.
The [installed-wheel check](../tests/test_provider_examples_installed_wheel.py)
runs those cases outside both repositories with network connections forbidden.
