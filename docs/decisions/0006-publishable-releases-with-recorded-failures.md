# Decision 0006: a release may be publishable with recorded failures, if every one is deterministic and enumerated

- Date: 2026-09-04
- Status: **accepted**, ruled by the product owner 2026-09-04. Not implemented;
  the four locks below must be lifted first, in the stated order.
- Raised-by: `0002-shared-execution-and-the-acquisition-gap-ledger.md`'s retry
  ruling, which cannot be executed while a failure has nowhere to be recorded.
- Its own record, not a section of 0002, because 0002 rules what a run *retries*
  and this rules what a release *means*. Those move independently and a reader
  looking for one should not have to read the other to find it.

## The ruling

A source-native release **may be published while carrying recorded failures**,
when all three hold:

1. **Every failure is deterministic-class.** The class boundary is ruled in
   `0002` (`a333382`) and is not restated here: the dividing question is whether
   the identical unchanged request could plausibly succeed.
2. **Every failure is enumerated in the acquisition ledger with a disposition.**
   A count is not enough. The release must say which items failed and why.
3. **No consumer caches it as a permanent fact about the item.** See *The
   sentence that keeps supersession working*.

**A release carrying any transient failure stays unpublishable.** A transient
failure means acquisition is not finished, and a snapshot taken mid-retry
describes the network rather than the source.

This applies the 2026-09-02 record-granularity precedent — keep the object in
evidence, contribute no record, count it — at release granularity.

## Why "complete-snapshot with gaps" is coherent

The objection to this ruling is that a complete-snapshot release with missing
records is a contradiction. It is not, and the difference is exactly the one this
platform has now made twice.

**Silent omission falsifies the claim. Enumeration preserves it.** A snapshot
that says *"these N items exist, and here is why each carries no record"* is
complete evidence of what the source served: a reader can reconstruct the
population and audit every absence. A snapshot that quietly has N fewer rows is
not complete evidence of anything — it is indistinguishable from a snapshot of a
smaller source, which is the property that makes it a lie rather than a gap.

This is the same principle that made yesterday's ingest exclusion wrong and
today's ledger entry right. The corpus is allowed to lack a document. It is not
allowed to lack the fact that it lacks the document.

## The asymmetry of misclassification, and why it is named here

The two directions of error do not cost the same, and the ruling depends on
which one is expensive.

- **Deterministic called transient**: the run retries something that will fail
  again. Cost is a retry.
- **Transient called deterministic**: a recoverable gap becomes a permanently
  recorded absence, **in a release that is publishable precisely because of the
  misclassification**. The error and the licence to publish are the same act.

The class is assigned from one attempt's evidence, so this error is available and
will occur. **It is the expensive one and it is the one to design against.**

**The mitigation is supersession, and it is why the ruling is safe.** Releases
are immutable and supersedable. A later attempt that succeeds corrects the record
by publishing a new release, not by mutating the old one — so a misclassification
is a temporary wrong answer with a defined route to a right one, rather than a
permanent corruption. That route is the whole safety argument, which is why the
next section exists.

## The sentence that keeps supersession working

**No consumer may treat a deterministic failure as a permanent fact about the
item.** Not a catalog, not a snapshot, not an index, not a derived store.

A consumer that records "this item has no body, do not ask again" breaks
supersession at the only point where it matters: the corrected release publishes,
and nothing downstream asks. The failure outlives the fact that produced it, and
the mitigation above becomes a mechanism nobody exercises.

This is forbidden **before** anyone builds it, because it is the natural
optimisation. The first consumer to notice it is re-requesting known-failed items
will propose exactly this cache, and it will look like a performance win.

A deterministic failure is a fact about **one acquisition attempt under one
policy version**. It is never a fact about the item.

## What blocks it: four locks, and the order they lift in

Verified against `spicy-docs` on 2026-09-04. Cited by symbol; three of the four
sit in `source_native.py`.

| # | lock | where | group |
| --- | --- | --- | --- |
| 1 | the ledger cannot express a failure — `"failure": {"type": "null"}` | acquisition-ledger schema | step 1 |
| 2 | the verifier **recomputes** the count block with `"failedRecordCount": 0` and compares | `verify_source_native_release` | step 1 |
| 3 | admission refuses nonzero **in the same condition as a failed semantic verdict** | `verify_source_native_admission` | step 1 |
| 4 | the publisher hard-codes `"failedRecordCount": 0` | `_publish_indexed` | step 2 |

**This was counted three ways before it held, and the history is kept because it
is the useful part.** The first brief named three locks. spicy9 found the
same-condition refusal, making four. A verifier recomputation then surfaced,
apparently making five — until spicy9, re-reading their own report, found that
the "second writer" and the verifier were **the same function**: the second
hard-coded zero sits inside `verify_source_native_release`, not at a second
publish site. So the verifier was already among the four, hiding inside a
mislabel, which is why it presented as a discovery rather than a correction.

Four is the count. **Step 2 is one writer site, not two.**

The point that survives all three counts is the one worth carrying: **the number
is not what matters, the grouping into steps is.** A fifth would not have changed
the order, and a reader arriving at "four" should be able to see it was counted
three ways before it settled rather than assume it was obvious.

### The order, corrected

**An earlier revision of this record said "readers first, then schema, then
writers". That order is not shippable and spicy9 caught it.** The reader at step
1 is supposed to check the failure class — but the class field does not exist
until the schema permits it, so a reader validating against the current schema
would **refuse the very releases step 3 produces**. Reader-first, applied
literally, causes the breakage reader-first exists to prevent.

The corrected order:

1. **Widen the schema to permit both shapes, and ship the reader and the verifier
   together.** The ledger accepts `failure: null` with a zero count *and*
   `failure: {class, reasonCode, evidence}` with a nonzero one. The reader accepts
   both while still refusing any nonzero count where a failure is transient or
   unclassed. The verifier stops recomputing the count as a constant.
2. **The writer emits the real count and the class** — one site, `_publish_indexed`.
3. **The first honest release.**

The principle survives intact — **nothing may be written before something can
read it** — but what defers to step 2 is the writer-side *use* of the new shape,
not its permitted existence. Permitting a shape breaks nothing; emitting one
does.

**The verifier belongs in step 1 beside the reader, and that is why step 1 is a
group rather than a file.** `verify_source_native_release` recomputes the count
block rather than reading it, so a publisher emitting an honest count fails its
own verifier independently of any reader. Left to step 2 as a writer concern, it
is missed, and the first honest release fails verification at step 3 — after
everything else looked correct.

**On its own release, not the Federal Register one.** The Federal Register
changes in `0003` alter what a release *contains*; this alters what a release
*means*. Shipped together, the first downstream refusal has two suspects and
neither can be cleared without unpicking the other. spicy9 holds the build order;
this sequences after their prerequisite work, not inside it.

## What this does not decide

- **The class boundary.** Ruled in `0002`; referenced, not restated.
- **How a disposition is spelled** in the ledger — the reason-code vocabulary is
  a schema question for the commit that lifts lock 1.
- **Whether a retry is automatic.** That is `0002`'s ruling; this record only
  says what a release may carry once one has been attempted.
- **Anything about the Federal Register rebuild**, which is `0003`'s.
