# Core model editor's draft: architecture review

Reviewed 2026-09-13 against the working tree at `b9b254d` plus the untracked
`docs/core-model.md`, `docs/core-model-implementation-plan.md`, and the
modified `docs/documentation.md`. Method: semi-formal architecture review
(numbered premises, observations, invariants, findings; verdict derived from
the findings). W3C claims were checked against the published PROV-DM,
PROV-CONSTRAINTS and PROV-Dictionary texts on the review date. The reviewer
read source and documents; no tests were executed and no source was changed.

**Verdict: RECONSIDER (D2).** The model's substance is sound and its PROV
grounding is correct in every checked particular but one. It fails on
placement: it introduces a third normative vocabulary with no declared
authority tier, no mapping to the concepts the code and the 2026-08-05
standalone spec already define, and a companion implementation plan that
contradicts the September 12 record-storage decision and never mentions the
existing implementation.

**Owner ruling, 2026-09-13, after reading this review.** Do not get hung up
on authority and ceremony. Refine `docs/core-model.md` to be the best version
of itself: referenceable, defensible, and a guard against context drift.
Changing DocSpec to fit the spec is acceptable. Findings 1, 6 and 7 are set
aside as ceremony. Findings 2, 4, 5 and 8 were applied to the spec the same
day: a complete PROV binding table, a dated Appendix D mapping model terms to
runtime names with the gaps listed, a term index, rationale notes citing
Decision 0002, and the drafting fixes. Finding 3 stands unapplied: the
implementation plan still contradicts the record-storage decision.

## 0. Coverage and observation log

### Artifacts read

| Artifact | O-IDs | Key observations |
| --- | --- | --- |
| `docs/core-model.md` (full) | O1–O16 | See below |
| `docs/core-model-implementation-plan.md` (full) | O24 | See below |
| `docs/documentation.md` and its diff | O17, O18 | Both drafts filed 2026-09-13 "unchanged" as "target semantics" |
| `docs/decisions/README.md`, `0002` §What DocSpec is for | O18, O19 | Authority tiers; purpose statement; formatting-assertion lesson |
| `docs/architecture.md`, `docs/experiments.md`, `docs/record-storage.md` | O21, O23 | Existing vocabulary; Parquet/DuckDB decision; open identity cleanup |
| `docs/superpowers/specs/2026-08-05-…-implementation-spec.md` §3, §5.3, §8.4 | O22 | Sibling defining core concepts and MUST-level reuse rules |
| `docs/dataset-experiments-todo.md` lines 30–105 | O21 | Governing decisions; no identity-cleanup item |
| `src/docspec/domain/content.py:214–224`, `domain/processors.py:125–150, 395–425, 750–770`, `application/base_reprocessing.py`, `application/execution_evidence.py:300–370` | O20 | Existing capture correspondence, reuse key, reuse receipt, determinism policy |
| W3C PROV-DM, PROV-CONSTRAINTS, PROV-Dictionary (live fetch) | O1, O3–O7 | Dates, terms, semantics verified |

### Artifacts not read

- `docs/dataset-experiments-todo.md` beyond lines 30–105 and targeted greps
  (provenance, identity, overlay, PROV). Safe: the greps found no item
  requesting a provenance model or an identity cleanup.
- Standalone spec sections other than §3, §5.3, §8.4. Safe: the finding is
  that a sibling exists and is uncited; its full content does not change that.
- Codex session logs for 2026-09-13: none exist. The drafts' origin is not
  recoverable from this machine (O26).

### Recorded searches

- `grep -rn -i "core-model|prov-dm|PROV\b|prov:"` over `*.md *.py *.toml`,
  excluding the two drafts: only `docs/documentation.md` matches.
- `git log --all -S "PROV-DM" -- docs` and `-S "Keyed-State"`: no commits.
  `-S "application profile"` matched the two founding commits, but
  `git grep -i "application profile" e7b577b -- docs` finds nothing, so this
  is a non-docs or transient match. No prior PROV-based draft exists.
- Vocabulary map (files in `src/`, files in `docs/*.md` excluding the drafts):
  overlay 0/0; reuse association 0/0; correspondence 0/0; declared
  dependency 0/0; operation definition 0/0; raw data 0/0; occurrence 2/2;
  dataset state 2/1; retained 37/33; pinned base 5/4; supersede 10/13.
- Plan searched for `rulespec|parquet|existing repo|existing code|
  architecture.md|record-storage|blob store|content-address|current
  implementation|already implement|.blobs|spicy`: one AiiDA line and one
  DuckDB capability row match; nothing refers to the existing implementation.

### Hypotheses

- H1 (the PROV bindings and citations are correct): REFINED. All dates,
  Dictionary terms, insertion semantics, Constraint 37 and the derivation
  direction check out. One binding row names a construct PROV-DM lacks (O1).
- H2 (the model describes what the code already does): CONFIRMED in
  substance (O20), REFUTED in vocabulary (O25). The code embodies the
  distinctions; the spec does not name the code's concepts.
- H3 (the implementation plan serves the spec and the repo): REFUTED (O23,
  O24).
- H4 (the document has a declared authority tier): REFUTED (O17, O18).

## 1. Artifact summary

- **Path**: `docs/core-model.md`, 429 lines, untracked; filed via
  `docs/documentation.md:17–18, 48–51`.
- **Stated problem**: none stated. The Abstract opens with the decision.
- **Stated decision**: define an application profile of W3C PROV for
  constructing, capturing, transforming and revising datasets, adding
  identifiable membership, retained data, operation descriptions, declared
  dependencies and policy-controlled reuse (`core-model.md:8`).
- **Category**: proof infrastructure with a research framing. No user-visible
  behavior changes.
- **Subsystems touched**: none in code. Documentation index only.
- **Prior artifacts cited**: W3C PROV family, BCP 14, DCAT 3, RO-Crate,
  RFC 6901/6902. No DocSpec artifact.
- **Should have cited**: Decision 0002 §What DocSpec is for; standalone spec
  §3 and §8.4; `docs/record-storage.md`; `docs/experiments.md` naming table.
- **Implementation status**: pre-implementation as a document. The
  behaviors it specifies are largely landed under other names (O20).

PREMISES:
- P1: DocSpec defines an application profile of W3C PROV (`core-model.md:8`).
- P2: The model supports arbitrary root values, non-destructive revision,
  independently referenceable results and selective reuse without
  prescribing storage, serialization, orchestration or execution
  (`core-model.md:10, 24`).
- P3: Conformance needs only recoverable identities and relationships;
  concepts may share physical records; no RDF, graph store or inference
  engine is required (`core-model.md:20, 49`).
- P4: The binding table is normative and "uses the types and relationships
  defined by PROV-DM"; the recoverable PROV interpretation MUST satisfy
  PROV-CONSTRAINTS (`core-model.md:28, 30, 43`).
- P5: The drafts are "target semantics", distinct from `architecture.md`'s
  current behavior (`documentation.md:48–51`).
- P6 (plan): "For a rapid, faithful implementation of the current spec, I'd
  start with Python, a transactional metadata store, an existing artifact
  store, and the Python `prov` library"; default stack Pydantic +
  SQLAlchemy/SQLite + disk-objectstore + `prov`
  (`core-model-implementation-plan.md:1, 203`).

## 2. Lineage and relationship

### Lineage

| Prior artifact | Type | Relation | Citation |
| --- | --- | --- | --- |
| Decision 0002 | parent (purpose) | Encoded but uncited: §5.3's "absence of an output alone MUST NOT be interpreted as success or failure" is the GovInfo 200-with-empty lesson; §6.3's "MUST NOT be fabricated or inferred solely from current values" is the formatting-assertion lesson | `0002:51–58, 99–110`; `core-model.md:178, 236` |
| Standalone spec 2026-08-05 §3, §8.4 | sibling | Overlapping normative scope, uncited | `…-implementation-spec.md:187–284, 1237–1256` |
| `docs/record-storage.md` | constraint | Contradicted by the plan | `record-storage.md:1–12`; `decisions/README.md` final paragraph |
| `docs/experiments.md` naming table | sibling | Existing vocabulary, unmapped | `experiments.md:17–28` |
| (none) | — | No prior PROV draft in history | recorded search above |

### Relationships

| Component / seam | Owner | Depends on | Depended on by | Seam named? |
| --- | --- | --- | --- | --- |
| Core model vocabulary | undeclared | PROV-DM, PROV-CONSTRAINTS | nothing yet | no |
| Repo vocabulary (SourceCatalog, SourceItem, CapturedFile, Representation, Segment, Processor, DocumentRelease, receipt) | domain + standalone spec §3 | Rulespec container | all code, tests, guides | yes: `…-implementation-spec.md:187–284`, `experiments.md:17–28` |
| Reuse rules | standalone spec §8.4 and `domain/processors.py` | processor determinism flag | `base_reprocessing.py`, `execution_evidence.py` | yes: `…-implementation-spec.md:1237–1256` |
| Record storage | `record-storage.md` | Parquet, DuckDB | every retained layer | yes |

### Stated intent vs actual shape

- **Stated intent**: a general model DocSpec conforms to (P1, P2).
- **Actual shape**: a standalone PROV profile with its own vocabulary,
  no DocSpec binding, no authority tier, and a companion plan for a
  from-scratch implementation.
- **Divergence**: MAJOR against P2 and P6. P2 says the model supports what
  DocSpec does; O25 shows zero shared normative vocabulary. P6 plans an
  implementation for a spec whose behaviors the repo already implements
  (O20) on a storage stack the repo already decided against (O23).

## 3. Invariants and commitments

INVARIANT 1:
  Statement: One maintained documentation tree; nothing becomes a second
  current-behavior authority; explanations link to owners without creating
  a competing specification.
  Source: `documentation.md:62–66, 80–84` (advisory tier, maintained guide).
  Status: RELIED-UPON by the "target semantics" label, but strained.
  Evidence: O12 (65 MUST-level requirements), O17, O18.
  Failure mode: a reviewer or agent treats a core-model MUST as a repo rule
  and files a gap against code that never accepted it. Exercised by the
  to-do process requiring an architect review for judgment items
  (`dataset-experiments-todo.md:95–99`), which reads `documentation.md`.

INVARIANT 2:
  Statement: Accepted decisions establish the intended rule; code and tests
  establish current behavior; a disagreement is a recorded gap.
  Source: `decisions/README.md:8–11` (load-bearing).
  Status: NEWLY-INTRODUCED third tier ("target semantics") with no home.
  Evidence: O17, O18.
  Failure mode: no procedure says whether a core-model/code disagreement is
  a gap to record or a draft to fix.

INVARIANT 3:
  Statement: Record layers are Parquet queried by DuckDB; SQLite serves
  caches and scratch; raw bytes stay in the content-addressed blob store.
  Source: `record-storage.md:1–12, 79–84`; `decisions/README.md` final
  paragraph (load-bearing, independently reviewed, 1,129 tests).
  Status: BROKEN by the implementation plan.
  Evidence: O23, O24.
  Failure mode: a reader following `documentation.md:18` plans a second
  storage stack. Exercised by that index row.

INVARIANT 4:
  Statement: The recoverable PROV interpretation is valid under PROV-DM and
  PROV-CONSTRAINTS.
  Source: `core-model.md:28` (newly introduced by the artifact).
  Status: NEWLY-INTRODUCED and under-specified.
  Evidence: O1 (one non-PROV binding), O13 (results and reuse associations
  have no stated interpretation).
  Failure mode: two implementations produce different PROV graphs for the
  same DocSpec description and both claim conformance. Nothing exercises
  this today; OBSERVATION severity.

INVARIANT 5:
  Statement: Every obligation is a checkable rule against a named file and
  line; absent rules are recorded absences.
  Source: Decision 0001 and 0002 conventions (`0001:19–23`, `0002:20–22`).
  Status: not followed. The core model names no file, test, or check, and
  §1 line 22 removes execution from conformance, so no conformance suite is
  implied.
  Evidence: O16.
  Failure mode: the six-month critic's question in Section 5.

## 4. User-value analysis

- **User-visible outcome**: none now. No behavior, format, or API changes.
- **Beneficiary**: contributors and agents who need one stable vocabulary
  for identity, retention, reuse and supersession; later, PROV or RO-Crate
  interop consumers (Appendix C).
- **Conceptual debt added**: a third normative vocabulary alongside the
  standalone spec §3 and the `experiments.md` naming table; an undeclared
  authority tier; a plan that contradicts an accepted storage decision.
- **Conceptual debt paid down**: potentially the open identity cleanup
  (O21), if the model is bound to the code's identities. Not yet.
- **Category**: proof infrastructure.
- **Falsifiability claim**: in six months, no code, test, decision, or guide
  cites a core-model section number, and standalone spec §3/§8.4 still
  govern. Then this document delivered nothing.
- **Smaller-shape candidates**:
  - A normative "DocSpec binding" section mapping each term to the existing
    concept, plus retirement of standalone spec §3/§8.4 or an explicit
    supersession: still open, recommended.
  - Fold the eight load-bearing distinctions into a Decision and drop the
    profile framing: viable, loses the PROV interop path.
- **Greenfield-first check**: YES for the contract. The distinctions the
  spec draws (execution completion vs successful retention vs availability
  vs eligibility; occurrence identity vs value; raw/derived as contextual
  roles; reuse is not re-execution; declared vs actual dependency) are the
  ones the code already enforces (O20). The spec is right in substance and
  wrong in placement.

## 5. Counterfactual analysis

- **Kill criterion**: a required DocSpec behavior the model cannot express.
  Checked Decision 0004's cross-filed collapse: two records with one key
  collapse into one item with a recorded discard. §3.3 permits it via a
  "defined addressing rule"; §9.2 forbids only collapsing equal values at
  different keys. Not triggered. No kill found.
- **Counter-decision shape**: no general model; keep standalone spec §3 and
  §8.4 plus decisions. Simplifies to one vocabulary. Loses the PROV
  grounding and the clean four-way status distinction of §5.3.
- **Removal probe**: delete both drafts; `documentation.md:17–18` break.
  Nothing else references them (recorded search). Silent failure.
- **Sibling subsumption**: PARTIAL. Standalone spec §3/§8.4 covers concepts
  and reuse rules. The core model adds PROV grounding, the
  successful-retention contract (§5.2), discovered omissions (§6.3), and
  the keyed-state profile (§9), none of which the sibling has.
- **Six-month critic**: "Which of these 65 MUSTs does the code satisfy, and
  where is that checked?"

OPPOSITE-VERDICT PROBE:
  If APPROVE-as-filed were correct, there would be a decision or to-do item
  asking for a PROV-based model, code or tests citing it, or a mapping to
  existing concepts.
  Searched for: git history for PROV-DM and Keyed-State; repo-wide grep for
  core-model and PROV outside the drafts; the to-do list for a provenance
  model or identity cleanup item; the draft for any DocSpec citation.
  Found: nothing. Only `documentation.md` references the drafts. The draft
  cites no DocSpec artifact (O16).
  Conclusion: REFUTED.

## 6. Findings

FINDING 1:
  Severity: CONCERN
  Category: ownership
  Location: `docs/core-model.md:1–4`; `docs/documentation.md:17, 48–51`
  Description: Sixty-five MUST-level requirements enter the maintained tree
  with no authority tier, no author, no date, no status, no Accepted-by and
  no "what this does not decide".
  Evidence chain: O8, O12, O17, O18; INVARIANT 1, INVARIANT 2.
  Exercised by: the to-do's architect-review process reading
  `documentation.md` (`dataset-experiments-todo.md:95–99`).
  Recommendation: RESHAPE. Add a Status block modeled on the standalone
  spec's `## Status` (`…-implementation-spec.md:7`) and Decision 0001's
  header conventions: date, source of the draft, accepted-by or "proposed",
  what it supersedes, what it does not decide. Until accepted, either keep
  it under `docs/` labeled proposed with that block, or move it to
  `docs/history/2026-09-13-core-model-draft.md`.

FINDING 2:
  Severity: CONCERN
  Category: sibling-conflict, intent-vs-shape
  Location: `docs/core-model.md` §2–§7; standalone spec §3 and §8.4
  Description: The model's normative vocabulary shares no term with the
  code or with the standalone spec, which already defines the same concepts
  and reuse rules at MUST level. Two competing normative vocabularies for
  one system, and P2's claim to describe DocSpec is unsupported by the text.
  Evidence chain: P2; O20, O21, O22, O25.
  Exercised by: any contributor choosing between `experiments.md`'s
  "retained result" and the model's "successfully retained result" when
  naming a field or test.
  Recommendation: RESHAPE. Add a normative DocSpec binding section. Minimum
  rows, each citing the owner:
  root dataset state → sealed `SourceCatalog`;
  root-record occurrence → `SourceItem`;
  capture execution and raw data → capture stage, `CapturedFile`, capture
  receipt;
  transformation execution → extractor, segmenter, processor invocation;
  operation definition → `ProcessorDescription` identity and configuration
  digests (`processors.py:762`; `base_reprocessing.py:107–115`);
  declared dependency for capture → `SourceItem.same_acquisition_inputs`
  (`content.py:214–224`);
  declared dependency for processing → `ProcessorRequest.reuse_content`
  (`processors.py:401–418`);
  reuse policy → `ProcessorCachePolicy` and the deterministic flag
  (`processors.py:759–760`);
  reuse association → `docspec-processor-invocation-receipt` with
  `cacheDisposition: reused-base` and `result` naming the selected result
  (`base_reprocessing.py:260–263`; `execution_evidence.py:366–367`);
  dataset state → `DocumentRelease` active layers;
  supersession → `previousRelease` and `supersedes`, never rewritten by
  `select` (`experiments.md`).
  Then record the gaps honestly: no overlay or keyed state (§9); no
  projection definition object distinct from the code path that projects;
  declared dependencies not recorded separately from input bindings; no
  §6.3 qualification record. Finally state whether this model supersedes
  standalone spec §3 and §8.4 or defers to them.

FINDING 3:
  Severity: CONCERN
  Category: commitment-violation, sibling-conflict
  Location: `docs/core-model-implementation-plan.md:1–11, 26–29, 58, 203`;
  `docs/documentation.md:18`
  Description: The plan proposes a from-scratch stack, SQLAlchemy/SQLite as
  the authoritative ledger, disk-objectstore for content, the `prov`
  library, and an AiiDA evaluation, for a repository that decided on
  Parquet/DuckDB on September 12, has a content-addressed blob store and
  Rulespec containers, and already implements the spec's central reuse and
  retention distinctions. The plan never mentions the existing
  implementation.
  Evidence chain: P6; O20, O23, O24; INVARIANT 3.
  Exercised by: `documentation.md:18`, which sends a reader to this file to
  "plan the Core reference implementation and component choices".
  Recommendation: REVERSE as the implementation plan. Replace with a gap
  analysis against current code: which core-model requirements the code
  satisfies (the citations in FINDING 2), which it does not, and for each
  gap whether the abstraction is the only route to a needed capability.
  Keep two paragraphs from the plan as design notes: the reuse-index shape
  ("the fingerprint finds candidates; it does not become the artifact's
  identity", plan §4) and the publish-order rule (content first, metadata
  transaction second). Consider the `prov` library only for an export
  binding under Appendix C, never as the internal model.

FINDING 4:
  Severity: OBSERVATION
  Category: unstated-assumption
  Location: `docs/core-model.md:37, 43, 45, 99`
  Description: The normative binding table names "Specialized Activity",
  which PROV-DM does not define; specialization is entity-only. Line 43
  then claims every binding uses PROV-DM types. Operation results and reuse
  associations receive no PROV interpretation at all, though §2 requires
  the recoverable interpretation to be valid.
  Evidence chain: O1, O2, O13; INVARIANT 4.
  Recommendation: RESHAPE. Row 37 becomes "Activity, distinguished by a
  DocSpec `prov:type`". Line 43 is qualified accordingly. Add one sentence
  each: a result is either an Entity or attributes of its Activity; a reuse
  association has no PROV relation of its own and appears only as the new
  state's membership of the reused output.

FINDING 5:
  Severity: OBSERVATION
  Category: unstated-assumption
  Location: `docs/core-model.md:202, 234, 258, 264`
  Description: Four drafting defects in normative text. Line 234 uses "the
  qualification", never defined. Line 202 uses "MAY be removed only", a
  permissive keyword carrying a prohibition; the intent is "MUST NOT be
  removed except under an explicit policy". Line 264 uses lowercase "must"
  after §1 declared lowercase non-normative. Line 258's "a change outside
  those dependencies MUST NOT, by itself, make a result ineligible" reads
  over undeclared material dependencies and collides with line 224 unless
  the reader carries "subject to Section 6" forward.
  Evidence chain: O9, O10, O11.
  Recommendation: RESHAPE the four sentences. Line 258: "A change outside
  the adequately declared dependencies (Section 6.2) MUST NOT, by itself,
  make a result ineligible."

FINDING 6:
  Severity: OBSERVATION
  Category: ownership
  Location: `docs/core-model.md` lines 14, 28, 43, 127, 135, 186, 314, 318,
  336, 409, 411, 413, 430; `docs/core-model-implementation-plan.md`
  Description: Thirteen sentences in each file end in a stray trailing
  space, the residue of inline citation markers stripped from a chat
  assistant export. A provenance specification whose own provenance is
  unrecoverable is an irony worth removing.
  Evidence chain: O8, O26.
  Recommendation: RESHAPE. Record the origin in the Status block from
  FINDING 1 and strip the whitespace.

FINDING 7:
  Severity: OPTIONAL
  Category: debt-accretion
  Location: `docs/experiments.md:32–35`; `docs/dataset-experiments-todo.md`
  Description: The "broad identity cleanup" is named open with no to-do
  item, and the core model's occurrence-versus-value and request-versus-
  result distinctions are exactly its subject. Neither points at the other.
  Evidence chain: O21.
  Recommendation: RESHAPE. Add a to-do item that names the core-model
  binding from FINDING 2 as the cleanup's specification, or the model
  becomes another orphan.

FINDING 8:
  Severity: OBSERVATION
  Category: scope (validated correct)
  Location: `docs/core-model.md:117, 133–135, 178, 186, 236, 314–318, 336,
  423–426`
  Description: Every other checked claim holds. PROV-DM, PROV-CONSTRAINTS
  and PROV-Dictionary dates are correct. The Dictionary terms, the
  insertion update semantics, and the completeness statement match the
  Note. §4.5's "usage plus generation does not establish derivation"
  matches PROV-DM's "necessary but not sufficient" and the one-way
  Inference 11. §5.4 matches Constraint 37. §4.3's acyclicity is a DocSpec
  addition, correctly not attributed to PROV, which leaves derivation cycles
  unaddressed. §5.3 and §6.3 encode Decision 0002's two hardest lessons.
  Evidence chain: O3, O4, O5, O6, O7, O19.
  Recommendation: KEEP. Cite Decision 0002 at lines 178 and 236.

## 7. Verdict

DEFINITIONS:
  D1 APPROVE: no finding above OBSERVATION; invariants preserved; value
  supported; no-counterexample argument stated.
  D2 RECONSIDER: at least one CONCERN or BLOCKER has a concrete reshape that
  preserves intent.
  D3 REJECT: a BLOCKER admits no reshape, or a sibling fully subsumes.
  D4 NEEDS DISCUSSION: exactly one open question gates the verdict.

Precedence D3, D4, D2, D1. D3 fails: no BLOCKER, subsumption is PARTIAL.
D4 fails: no single gating question. D2 holds.

VERDICT: RECONSIDER — by D2

Justification:
  - Intent vs shape: DIVERGES — FINDING 2, FINDING 3 (P2, P6; O20, O24, O25)
  - User-value claim: WEAK — FINDING 2, FINDING 7 (no consumer cites it)
  - Commitment status: DRIFTING — FINDING 1 (INVARIANT 1, 2); BROKEN in the
    plan — FINDING 3 (INVARIANT 3)
  - Conceptual debt delta: ACCRETES — FINDING 1, FINDING 2
  - Sibling subsumption: PARTIAL — FINDING 2 (standalone spec §3, §8.4)
  - Opposite-verdict probe: REFUTED
  - Confidence: HIGH for the PROV validation and the placement findings;
    MEDIUM for the binding rows in FINDING 2, which were derived from
    reading five source files and should be confirmed by whoever writes
    the section.

Required reshape, in order:
  1. Replace `core-model-implementation-plan.md` with a gap analysis
     against current code that respects `record-storage.md` (FINDING 3).
  2. Add the normative DocSpec binding section and state the relation to
     standalone spec §3 and §8.4 (FINDING 2).
  3. Add the Status block declaring origin, date, and proposed-or-accepted
     status; strip the citation residue (FINDINGS 1, 6).
  4. Fix the five precision defects (FINDINGS 4, 5) and cite Decision 0002
     (FINDING 8).

After steps 1–3 the model is a credible candidate for acceptance as the
vocabulary the identity cleanup has been missing.

## Observations referenced above

- O1: `core-model.md:37` binds "Capture or transformation execution" to
  "Specialized Activity". PROV-DM §5.5.1 defines `specializationOf` for
  entities only.
- O2: `core-model.md:43` claims all bindings use PROV-DM types.
- O3: `core-model.md:133–135` matches PROV-DM ("a chain of usage and
  generation is necessary … not sufficient") and PROV-CONSTRAINTS
  Inference 11, which runs derivation → usage and generation only.
- O4: `core-model.md:186` matches PROV-CONSTRAINTS Constraint 37.
- O5: `core-model.md:117` acyclicity; PROV-CONSTRAINTS states no
  transitivity for `wasDerivedFrom` and does not address cycles.
- O6: `core-model.md:314–318, 336` match PROV-Dictionary: WG Note
  30 April 2013; `prov:hadDictionaryMember`, `prov:derivedByInsertionFrom`,
  `prov:derivedByRemovalFrom`; "a new pair replaces an existing pair with
  the same key"; "this key-entity-set is considered to be complete";
  "the complete content of a dictionary is unknown unless it can be traced
  back to an empty dictionary".
- O7: `core-model.md:423–426` dates verified: PROV-DM and PROV-CONSTRAINTS
  Recommendations 30 April 2013; RFC 2119 March 1997; RFC 8174 May 2017.
- O8: thirteen trailing-space sentence endings in each draft; no author,
  date, version, or status beyond "Independent Editor's Draft".
- O9: `core-model.md:234` "The qualification" undefined.
- O10: `core-model.md:202` "MAY be removed only"; `:264` lowercase "must".
- O11: `core-model.md:258` versus `:224`.
- O12: 43 MUST, 22 MUST NOT, 47 MAY; the sole SHOULD and SHOULD NOT are the
  §1 definitions.
- O13: `core-model.md:45` lists seven DocSpec-only concepts; `:99` binds
  operation definition to Entity outside the table; results and reuse
  associations get no interpretation.
- O14: `core-model.md:95, 155–162, 176–180` four-way status distinction and
  explicit empty outcomes.
- O15: Appendix A's overlay example has no repo analogue (overlay: 0 files).
- O16: the draft cites no DocSpec artifact.
- O17: `documentation.md:48–51` "saved from the supplied drafts on
  2026-09-13, with their contents unchanged … target semantics".
- O18: `decisions/README.md:8–11`; `documentation.md:62–66`.
- O19: Decision 0002 `:51–58` purpose; `:99–110` formatting assertion and
  GovInfo 200-with-empty.
- O20: `content.py:214–224`; `processors.py:401–418, 757, 759–760`;
  `base_reprocessing.py:260–263`; `execution_evidence.py:366–367`.
- O21: `experiments.md:17–35`; to-do grep for identity cleanup: none.
- O22: standalone spec `:187–284` (§3), `:890–898` (§5.3 semantic plan
  identity), `:1237–1256` (§8.4 Reuse).
- O23: `record-storage.md:1–12, 79–84`; `decisions/README.md` final
  paragraph.
- O24: plan `:1–11, 14, 26–29, 58, 86–110, 203`; recorded search above.
- O25: vocabulary map in Section 0.
- O26: no Codex session on 2026-09-13; no prior PROV draft in git history.

## Resolution, 2026-09-13

The owner set aside FINDING 1 and the status-block half of FINDING 6 as
ceremony, and directed that the spec be refined on its own terms: changing
DocSpec to fit the spec is acceptable, so the code binding in FINDING 2 and
the gap analysis in FINDING 3 belong to the implementation plan, not the
spec. Applied to `docs/core-model.md` the same day: FINDING 4 (the binding
table row; operation definition bound as a PROV Plan, moved from §4.1 into
the table), FINDING 5 (all four sentences), the whitespace half of
FINDING 6, one definition of "material" in §4.1, one undefined normative use
of "overlay" in §9.3, and removal of §4.1's duplicate list of required
result contents in favor of §5.2. FINDINGS 2, 3 and 7 remain open against
the implementation plan and the to-do list.
