# D22/D48: retain conditional integration tasks; do not build a generic host

Read-only architecture judgment, September 12, 2026. No tests or implementation changes.

**Recommendation:** defer D22 until a named source acquisition workflow is supplied. Treat D48 as composition and qualification of a supplied recipe using existing native Dagster and public artifact APIs. No additional DocSpec executor, generic recipe/task model, registry or run ledger is justified by current callers.

## Evidence

- D22 explicitly requires one supplied acquisition task and preserves ordering, cancellation, locks, bounded resources, pins and stale-resume refusal (`docs/dataset-experiments-todo.md`, D22). D48 explicitly permits a helper only for a real caller need not supplied by existing APIs (D48).
- `StoreTask` is one operation over a saved `DocumentStore`, not a generic computation (`src/docspec/domain/execution.py:166–182`). Saved runtime recovery rejects unsupported operations (`src/docspec/runtime/preparation.py:98`). Broadening that shape before a caller exists would introduce new state semantics without demonstrated benefit.
- `build_dagster_definitions` already accepts native resources, executor and retry policy, then maps bounded DocSpec document tasks (`src/docspec/adapters/dagster.py:94–178`). The prepared runtime remains the document-specific object. Native resources own construction and cleanup; no second dependency container is needed (`docs/history/2026-09-11-native-dagster-di-architecture.md`).
- The installed native example demonstrates capture and processing with retained inputs and real task messages (`examples/dagster_experiment.py:97–175`). Its guide explicitly limits the example to two result messages and does not claim a generic recipe host, large-dataset collector or hosted-deployment qualification (`docs/dagster-experiment.md`, final section).
- A non-document computation can already open the full admitted source catalog, including rows not selected for body acquisition (`src/docspec/runtime/catalogs.py`, `open_local_catalog`; `src/docspec/adapters/catalog_artifact/reader.py:64–80`). Retained results expose layer scans and lookups (`src/docspec/ports/document_catalog.py:19–25`). A native Dagster asset/job can consume these and use existing Rulespec artifact publication without inventing a capture or segment.
- Narrow current-caller search in DocSpec source/examples/tests found no supplied source-campaign task or non-document recipe integration. This is an observation about this checkout, not proof no such computation exists elsewhere.

## Destination evidence and one real follow-up

SpicyDocs S21 already defers campaign replacement until a named workflow demonstrates the required behavior; S31 explicitly reports no local dataset loop to replace (`../spicy-docs/docs/simplification-todo.md:121–137`). This agrees with D22 deferral and does not justify constructing a source-side caller merely to migrate it.

Search SC04 still waits for D48 to supply a “bounded dataset-stage API” and proposes delegating attempt/reuse/publication lifecycle (`../spicysearch/PLAN.md:2456–2464`). Current D48 instead requires native Dagster composition and no helper without observed need. This circular prerequisite should be corrected **in the destination Search SC04 task**: first expose the selected existing recipe's pinned inputs, outputs and native composition through its installed entry point, then qualify public DocSpec readers and any demonstrated missing seam. Source/search policy and semantic producer identity stay with the recipe.

No sibling implementation or TODO edit was made in this review. Parent owns any authorized destination task wording update.

## Reopening criteria

D22 needs an actual acquisition task and baseline evidence for ordering, root/lock ownership, bounded resources, interruption, retained reference identity and stale-resume refusal. Compare a thin native composition against that baseline; do not map source-publication work into fake document stores just to use DocSpec's executor. Keep the source publisher independently usable.

D48 needs a supplied retained-input computation and a metadata-only computation with explicit input/output pins, whole-build versus partition reuse, global census/lookup dependencies and observed calls/scans. Compose native jobs/assets/resources first. A small DocSpec helper is justified only if this exercise identifies a repeated DocSpec-owned rule that public readers/retention and shared publication cannot express. Then implement that exact rule and prove saved work; do not forecast an abstract host API.

Retain D49/D50 as conditional qualification and measured-simplification tasks. Existing document/Dagster evidence supports the substrate, but does not complete either source-campaign parity or generic recipe acceptance. A reference link or changed task wording is not newly passed execution evidence.

**Verdict: approve the narrower deferral.** Current integration value comes from shared readers and existing native execution, not another host. The next useful input is a concrete destination recipe/workflow. No additional DocSpec production change is justified now.
