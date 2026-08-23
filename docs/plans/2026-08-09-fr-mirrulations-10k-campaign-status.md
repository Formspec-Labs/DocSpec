# FR/Mirrulations 10k Qualification Campaign — Run Status

**Date:** 2026-08-09 (state as left by the 2026-08-05/06 implementation session)

**Plan:** [2026-08-06-federal-register-mirrulations-10k-real-world-sampling-plan.md](2026-08-06-federal-register-mirrulations-10k-real-world-sampling-plan.md)

## Where things stand

> **Read the addenda first.** The two dated addenda below (2026-08-21, 2026-08-23)
> correct this note. Everything above them describes the tree as it stood on
> 2026-08-06 and no longer describes what a restart would do.

- **Smoke tier: passed** — 100 documents / 136 candidates.
- **Intermediate tier: passed** — 1,000 documents / 1,359 candidates.
- **Full 10k tier: partial.** The run was intentionally killed at roughly 97,700 fetched files / 4.8 GB. Its planned-work ledger and candidate execution checkpoints were preserved, and this line originally claimed a restart would resume the durable plan and re-fetch nothing. It will not — see the 2026-08-21 addendum: every plan id written by that run is unreachable from current code, so a restart re-prepares and re-plans from scratch. Only the content-addressed blob store spares the refetch of bytes it already holds.
- **Test suite at time of stop:** 264 passed, 1 deselected.
- **Commits:** the work landed as two local commits on `main`:
  - `ae601c9` — `feat: add resumable mixed-source acquisition`
  - `8333768` — `feat: add mixed-source qualification campaign`

  As of this note they are **not pushed** to `origin/main`.

## Where the data lives

All run output is under `output/qualification/` (gitignored via `output/`), ~20 GB total:

| Directory | Size | What it is |
| --- | ---: | --- |
| `fr-mirrulations-10k-v1/` | 7.5 GB | **Live run.** `runs/` holds fetched documents and resume checkpoints; also `source-catalogs/`, `catalog-set.json`, `producer/`, `verification/`, `preparation.json`. |
| `fr-mirrulations-10k-v1-pre-direct-resume-fix/` | 9.4 GB | Snapshot taken before the direct-resume fix. |
| `fr-mirrulations-10k-v1-pre-runner-boundary-fix/` | 2.3 GB | Snapshot before the runner-boundary fix. |
| `fr-mirrulations-10k-v1-pre-gate-receipt-fix/` | 985 MB | Snapshot before the gate-receipt fix. |
| `fr-mirrulations-10k-v1-pre-routing-bound-fix/` | 97 MB | Snapshot before the routing-bound fix. |
| `fr-mirrulations-10k-v1-pre-local-fd-fix/` | 37 MB | Snapshot before the local-fd fix. |

The five `-pre-*-fix` snapshots (~12.7 GB combined) are stale backups made mid-session before each recovery fix. Nothing resumes from them; they can be deleted to reclaim space once the recovery path is trusted.

## Open threads

1. Push `ae601c9` and `8333768` to `origin/main` (or review first).
2. ~~Resume the full 10k tier from the preserved checkpoints~~ — not possible; re-prepare and re-run it to a closed, classified campaign report. The 2026-08-21 attempt got the smoke and intermediate tiers and stopped in the full tier.
3. ~~Delete the `-pre-*-fix` snapshot directories~~ — done 2026-08-21 (12.8 GB reclaimed).

## 2026-08-21 addendum: the sealed checkpoints cannot resume under current code

- The 2026-08-10 restart attempt (`output/qualification/full-tier-resume.log`)
  crashed immediately: `verification/gate-receipt.json` had been renamed to
  `gate-receipt.superseded-2026-08-10.json` that same minute, and every tier's
  execution manifest pins that receipt by path and digest.
- The rename is not the real break. Store identity is
  `stable_urn("document-store", {planId, logicalPartition, entryIds})`
  (`src/docspec/domain/jobs.py:249`), and `planId` covers the profile
  description digests and the source-catalog id, whose coverage embeds the
  Mirrulations draw path. Two correct fixes landed after the checkpoints were
  written -- `8d62094` changed two profile description digests, and
  `2df6e4f`/`8af75cc` moved the draw to a tracked fixture -- so every plan id,
  and with it every planned-store ledger and sealed store from 2026-08-06, is
  unreachable from current code. The gate receipt additionally seals the whole
  working tree, which has moved by twenty-odd commits.
- Resume therefore means: re-prepare under current code (fresh gate receipt,
  fresh catalogs, plans, and manifests) and re-run the tiers. The
  content-addressed blob store still dedupes refetched source bytes and
  identical derived objects; the orphaned run state stays in place. The stale
  sealed artifacts were renamed `*.superseded-2026-08-21` following the
  existing convention, and the five `-pre-*-fix` snapshots (12.8 GB, verified
  unreferenced by the live campaign directory and the repository) are deleted.

## 2026-08-23 addendum: what the 2026-08-21 re-run actually reached

- **Preparation re-ran under current code.** A fresh gate receipt was sealed at
  12:36:55, a fresh catalog set at 12:37, and `preparation.json` at 12:38; the
  2026-08-06 artifacts were renamed `*.superseded-2026-08-21` beside them.
- **The first `run-all` failed at 12:40** (`run-all-2026-08-21-attempt1.log`)
  with `docspec.errors.StaleBaseError: document catalog current release differs
  from the expected base`, raised from `commit_release` through
  `adapters/storage.py`. The retry at 12:41 logged to an empty
  `run-all-2026-08-21.log` and carried the tiers below.
- **Smoke re-passed** at 12:44:36 —
  `urn:docspec:document-release:v1:7c8d2c06…`.
- **Intermediate re-passed** at 12:53:52 —
  `urn:docspec:document-release:v1:a23a8392…`.
- **The full tier did not finish.** `runs/full/` has no `release-reference.json`
  and no `run-reference.json`; its last write is
  `runs/full/reconciliation/processor-results.sqlite3` at **13:41:49**, and the
  run was killed there. So the campaign stands exactly where the header says:
  two tiers passed, the full tier partial — this time against current code.

### The 2026-08-21 gate receipt is already invalid

Not stale in principle: invalid today, before anything resumes. The receipt
seals every non-excluded file in the tree (786 of them), and the two 2026-08-22
commits added five files that are not in that set — `ba74bbb`
(`tools/courtlistener_bulk_source.py`, `tests/test_courtlistener_bulk_source.py`,
`fixtures/courtlistener-bulk-v1/pins.json` and its two capture pages) and
`c2292aa`. Checked directly:

```
>>> validate_gate_receipt(
...     Path("output/qualification/fr-mirrulations-10k-v1/verification/gate-receipt.json"),
...     repository=Path(".").resolve(),
... )
docspec.errors.IntegrityError: qualification gate evidence differs from the current implementation
```

That is the receipt's job — it seals the implementation the campaign was run
against, and the implementation moved. Practically: the next full-tier attempt
re-seals the gate receipt first, and every tier's execution manifest, which
pins that receipt by path and digest, is re-prepared with it. Re-running the
full tier alone against the 2026-08-21 preparation is not an option.
