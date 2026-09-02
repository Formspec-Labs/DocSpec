# Source-native seam findings, 2026-09-02

Bounded findings from two reviews of DocSpec and spicy-docs on 2026-09-02: one
against the platform's written record, one against both codebases. They were
first written as rules 6, 7, 9, 10 and 11 of
`docs/decisions/0002-shared-execution-and-the-acquisition-gap-ledger.md` and are
moved here unchanged, keeping their original numbers so earlier citations still
resolve.

They are moved because they are not decisions. Each is a defect with a fix
attached, and they were in a decision record only because that was the document
open at the time. A findings register is the right home: the entries are
heterogeneous, short-lived, and a stale line number here is a nuisance rather
than a broken obligation.

Line numbers were correct against DocSpec `366864a` and spicy-docs `c941729`.
Both trees are moving; re-derive before acting.

## Findings

**Rule 6 — S3 gets the patience `168c6c4` gave httpx. LANDED, `b13a1de`.**
`sources/mirrulations.py:84` now sets `read_timeout=120`, and
`_retry_transient` at `:355-390` gives each key up to `_MAX_TRANSIENT_ATTEMPTS
= 14` jittered attempts layered above botocore's five rather than instead of
them. An earlier draft of this rule said that path "is unchanged"; that was
true when written and is false now.

One gap survives and inherits this rule's number: `s3_client()`
(`sources/mirrulations.py:90-92`), used for agency discovery, still carries no
timeout and no retry configuration while every sibling path does.

**Rule 7 — `supersedes` gets a writer.**
`spicy-docs/src/spicy_docs/source_native.py:145` declares it and `:1469` threads
it to `build_artifact_root`, and no caller anywhere in `src/`, `tools/`, or
`tests/` ever sets it. Every release published today has `supersedes=None`.
Until a writer exists there is no lineage chain, so there is nothing for a retry
release to attach to.

**Rule 9 — the key/body identity refusal moves to the shared replay parser.**
It currently sits in `regulations_gov_source_native.py:1691`, inside
`_iter_pages`, which is the live acquisition path only. `_parse_page_response`
(`:1120-1198`) — the parser an independent verifier replays — never compares the
object key to `source_record_id(record)`. So `verify` cannot detect the defect
the refusal exists to prevent, and spec `:189-191` overstate what independent verification proves. A guard on
the acquisition path is an operational check; a guard in the shared parser is a
release property.

**Rule 10 — admission recomputes the acquisition policy digest, not its name.**
`verify_source_native_admission` compares `acquisitionPolicyId` and
`acquisitionPolicyVersion` against the installed profile
(`source_native.py:1709-1716`) and never recomputes `acquisitionPolicyDigest`,
which happens only in full replay (`:2116-2127`). Six lines later the source
schema is compared byte-for-byte and its digest recomputed (`:1844-1852`), so
the asymmetry looks deliberate and is not: two policy-content changes landed on
2026-09-02 under an unchanged `ACQUISITION_POLICY_VERSION = "1.0"` — the query
window widening carried at `regulations_gov_source_native.py:1603`, and Federal
Register's new `observationSelection` block. Recompute the digest at admission,
and bump both policy versions.

**Rule 11 — the seam covers what the producer publishes, or stops claiming to.**
`spicyregs_source_profile` (`adapters/spicyregs_source_native.py:73-81`) knows
four profile names; spicy-docs publishes six. Neither `gao-product-pages` nor
`spicy-regs-public-comments` can reach DocSpec, and the public-comments table is
the source-supply plan's declared first rung of supply — so the first rung is
currently unconsumable. The names also differ across the seam
(`regulations-documents` in the producer CLI against `regulations-gov-documents`
in the adapter), which is a live trap for an operator. Either extend the adapter
or delete the unreachable sources and say so.

Separately, the seam's only executable proof pins a producer wheel at
`4cf1e82` (`tests/test_source_catalog_installed_wheel.py:28`), six
behaviour-changing commits behind spicy-docs HEAD. Re-pin it.

## What is not here

Rule 8, the Federal Register record identity, moved to
`docs/decisions/0003-federal-register-record-identity.md` instead. It changes a
sealed identity and needs an owner's ruling, so it is a decision rather than a
finding.
