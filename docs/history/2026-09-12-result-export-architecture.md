# Retained-result export: final bounded decision

Decision: export the retained active result as a self-contained consumer dataset
using Rulespec's installed artifact implementation. Export is optional and runs
no fetching, extraction, segmentation, processors or scheduler. Independent
architecture/code review approved the bounded shape; root owns validation and
logical commits. This note describes the implementation candidate, not test
results.

## Public boundary

`docspec.runtime.export_local_result(plan, workspace, release_ref, destination,
admission=..., document_release_producer=..., export_producer=...,
max_output_bytes=...) -> ArtifactPin` admits the existing retained result,
copies its active dependencies, admits the completed export, and publishes it
atomically without replacement.

`docspec.result_export.open_result_export(path, expected_pin=..., producer=...,
max_output_bytes=...) -> AdmittedResultExport` opens only the export directory.
The view is a context manager with `pin`, `summary`, `layer_kinds`,
`records(kind)`, `read_blob(reference, max_bytes=...)`, `open_blob(reference)`
and `read_evidence(reference)`. It owns only a disposable bounded SQLite index.
Consumers need no original workspace or live stage implementations.

## Two explicit content choices

- `retained-evidence` preserves valid active state, including capture-only
  results, exclusions and accepted failures.
- `nonempty-text` additionally requires each active selected item to have
  non-whitespace UTF-8 text and no terminal failure. Supported text kinds are
  text, visible-text and pdf-text; markup/image passthrough is not extracted text.

Both preserve every row. Refusal reports complete reason counts and at most ten
source-qualified examples. Promised stage completion and contradictory evidence
remain integrity checks regardless of the chosen text requirement. No policy
class hierarchy, retention fraction, semantic score or second disposition ledger
is introduced. Text observations do not claim semantic completeness.

## Container and exact identity

The installed `rulespec-artifacts==1.0.12` owns `build_artifact_root`,
`write_member_manifest`, `describe_member`, `admit_artifact`, `ArtifactInput`,
`ArtifactPin`, `MemberDescriptor`, `LocalMemberSource` and
`publish_directory_no_replace`. The outer format remains spicy-artifact/1.0.
The kind is docspec-result-export. The product spec is closed:

- schemaId: urn:docspec:result-export:1.0
- retainedArtifactDigest: exact retained-result artifact digest
- admissionId: urn:docspec:export-admission:<choice>:1
- populationScope: retained-active-result
- evidenceScope: active-output-and-stage-receipts

The sole input has role retained-result and the exact retained pin. Its physical
digest must equal retainedArtifactDigest. This deliberate bridge distinguishes
same-plan results with different actual outputs/failures: Rulespec logical input
identity otherwise includes the logical input digest, not its physical digest.
Rulespec derives both export identities; DocSpec adds no digest engine.

The small export.json index lists each active layer's original kind/schema,
member key and row count. One global active-result manifest describes it,
one ordered JSONL file per active layer, exact referenced output blobs, and
exact small control files. Schema identifiers in shared member descriptors are
absolute URNs; DocSpec's index preserves existing layer schema IDs. The shared
root and manifest use shared canonical bytes without line framing; DocSpec
JSONL/index/control payloads use their existing declared file framing.

## Dependency and evidence scope

Embed captured, representation and segment blobs; active stage receipt files;
and processor invocation result/prerequisite-result/owning-plan controls.
Follow only those typed fields, never arbitrary provider JSON. Original source
catalog, prior result, provider/resource and owning-plan source/base pins remain
external provenance. This is not a historical replay or executable workspace.

Generic membership/byte checks are shared. Active logical row checks reuse
verify_logical_release_layers. Pure retained stage ordering and unfinished
attempt checks were extracted from failure_frontier into execution_evidence,
where both repair and export now call them. Full processor verification uses
its actual owning plan and attempt ceiling; a failed attempt with no invocation
has no owning-plan reference and supplies no reusable result.

Existing exact segment-representation checks verify byte slices. A tiny shared
single-mapping extraction from processing.artifacts verifies every identity
mapping against retained capture bytes. Named derived transforms are explicitly
not replayed by the independent reader. Controls reuse the extracted existing
control-byte parser. No parallel parser, provider protocol or evidence ledger
was added.

## Bounds and publication

max_output_bytes includes root, manifest and payloads. Before shared admission
hashes payloads, bounded root preflight checks both root and manifest-declared
payload totals. JSON roots/indexes have a 1 MiB ceiling, rows/controls 8 MiB,
per-item metadata plus distinct typed control dependencies 64 MiB. Disposable
index input is limited to four times the artifact allowance.

The first reader reuses in-memory evidence primitives with an explicit 64 MiB
ceiling per representation, segment and captured source needed for an identity
mapping. Larger capture-only blobs remain streamed to the artifact bound.
Consumed local files are checked against their admitted descriptors before
bytes/rows are exposed. Early iterator closure releases files; it does not
promise a final mutation check for an abandoned read.

Build into an owned sibling temporary directory, fsync, seal and admit, then
publish with Rulespec's no-replace primitive. Same-pin repeated publication is
admitted and returns the same pin. A different destination refuses. Exceptions
remove only this invocation's temporary directory; a killed process may leave
an unpublished orphan, and retry starts fresh. No export checkpoint system.

## Quality evidence and no-legacy removal

Concrete challenge tests cover complete visible text hidden behind large
suppressed markup, empty visible input, missing whole blocks despite high byte
retention, invented duplicate block text, empty/truncated PDF page evidence,
and pinned versus unknown upstream truncation. The PDF case injects deterministic
provider pages; it is not qualification of an installed real PDF parser.

These cases distinguish exact byte/mapping failures and observed empty text from
semantic completeness that cannot be established. A complete useful HTML parse
can have under 1% retention; a parse omitting a critical block can exceed 90%.
The new export therefore has no retention floor. No old threshold is replaced
by a new universal threshold.

After current export qualification, delete the superseded portable/campaign
route and its exclusive support, schemas, fixtures and tests. Historical mint
reproduction and compatibility are not retention reasons. No old-format importer
or converter will be built. Keep current visible parsers, bounded segmenters and
runtime evidence primitives because they serve the supported experiment path.
The parent owns that separate logical removal commit and maintained docs.

## Required validation

Focused tests cover normal/capture/accepted-failure exports; exact repeats and
interrupted no-publication; wrong producer/pin/member/ref and late mutation;
a resealed understated root refusing before payload reads; a structurally
admitted but false segment slice; the distinct processor-control byte bound;
old and two simultaneous owning plans; same-plan differing-result identities;
and the concrete quality challenges above.

The installed probe reuses the existing visible-text runtime fixture, exports
without repeated processing, closes workers, makes original workspace and source
unavailable, opens rows/blobs/owning-plan/result controls independently, then
refuses tampering. Parent records actual gate results separately.
