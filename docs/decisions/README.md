# Decision status and precedence

Start with the [current architecture](../architecture.md). For a behavior change,
identify the affected format or workflow, then read the applicable accepted
decision and its later amendments. An explicit supersession governs that scope;
it does not silently replace unrelated parts of an older specification.

Code and tests establish what currently runs. Accepted decisions establish the
intended rule. A disagreement is an implementation gap to explain and resolve,
not permission to erase either source of evidence. Historical measurements and
[historical generated summaries](../history/2026-09-11-wiki-consolidation.md)
do not override accepted rules or current executable checks.

This index was checked against the local code on 2026-09-11. It distinguishes
repository behavior from upstream acquisition work and historical receipts.

| Decision | Governing scope | Current evidence and limits |
| --- | --- | --- |
| [0001: DocumentRelease 2.0](0001-document-release-2-0.md) | Retired campaign-specific portable publication format | Superseded September 12 by [result exports](../result-exports.md). The old builder/verifier, floor calibration and exclusive schema/fixture chain are removed. Original decisions and mint receipts remain dated history; no legacy reader or byte-for-byte reproduction is maintained. Current retained application state remains independent. |
| [0002: execution and acquisition gaps](0002-shared-execution-and-the-acquisition-gap-ledger.md) | Dataset experiments, retry classification, complete accounting of acquisition gaps, and the division between source acquisition and document processing | DocSpec supports capture and processing prefixes, checkpoint recovery, retained alternatives, changed-stage reuse, and unified inspection of work and results. Broader qualification remains checklist work; see the decision's current-status correction. Content-addressed storage alone does not prevent fetching bytes again. Upstream acquisition-ledger completion is not established by local tests. Apply the later retry ruling and 0006 when assessing source-native publication; this index does not claim upstream implementation or release. |
| [0003: Federal Register identity](0003-federal-register-record-identity.md) | Upstream composite record identity and recorded collapse; source text stays unchanged | The decision records a September 5 rebuild and its acceptance evidence. DocSpec resolves installed SpicyDocs profiles through `adapters/spicy_docs_source_native.py`; the transitional `spicy_regs` package fallback was retired on September 11. A checkout does not establish which producer package or external release a deployment uses. Verify the installed reader and artifact pins for a campaign. |
| [0004: cross-filed documents](0004-regulations-gov-cross-filed-documents.md) | One retained Regulations.gov document with discarded filings recorded | Implemented in the Regulations.gov policy; `tests/test_cross_filed_collapse.py` checks selection, retained source data, and deterministic accounting. This is distinct from Federal Register record identity. |
| [0005: publisher withholding](0005-publisher-declared-withholding.md) | Explicit withholding reason and receipt reason counts | Regulations.gov policy version `1.2.0` implements withholding interpretation; source-policy and catalog tests exercise it. Withholding evidence must not become a guess that an unavailable body never existed. |
| [0006: publication with recorded failures](0006-publishable-releases-with-recorded-failures.md) | A source-native release may carry enumerated deterministic failures; transient failures still prevent publication | Accepted, with upstream implementation still unverified here. DocSpec delegates source-native verification to the installed producer reader; it does not implement the four producer-side changes named in this decision. Keep the recorded unimplemented status until producer code, tests, and a pinned release establish completion. A failure describes one attempt and policy version, never permanent absence. |

When implementing a decision, update this index with code/check pointers and
state separately whether local implementation, qualification, and publication
have happened. Preserve original dated text and corrections in the decision;
link to the current status instead of rewriting history.
