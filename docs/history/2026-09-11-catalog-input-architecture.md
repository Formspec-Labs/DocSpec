# Build catalogs without assembling storage services or inventing source evidence

The local runtime will build and open the existing immutable source catalog from
a workspace, explicit catalog identity and producer, and a selected source policy.
It returns the existing build result or admitted reader. Catalog-only work creates
only source-catalog storage; it does not prepare document processing or select a
current result. The CLI retains its separate publication staging and command
receipt because those checks serve explicit destination publication.

Caller-supplied records will use one small, closed input shape: a record ID,
caller-issued version, title, arbitrary metadata, and existing candidate rendition
values. A bounded adapter snapshots these values and derives exact input and
schema digests. The caller explicitly names the source namespace, its version,
and the scope of the submitted snapshot. These facts describe supplied input;
they do not prove a publisher collection succeeded or any document was acquired.

A supplied-record policy maps this input through the existing catalog policy
interface and interpretation helpers. It preserves all raw fields, qualifies item
identities by source, records missing normalized fields as absent, and distinguishes
usable candidates from unavailable documents. Unknown metadata does not become
observed topics or invented publisher claims. The existing builder still performs
ordering, schema, count, byte, diagnostic, and artifact verification.

The snapshot has explicit record and canonical-byte bounds. The local builder
uses an explicit SQLite scratch allowance; the existing workspace gains a page
cap and construction cleanup. This allowance bounds its database and rollback
journal reservation, not every concurrent file in a process or machine.

Alternatives considered were a second catalog format, a generic field-mapping
language, and requiring local callers to mimic a provider-specific schema. Each
adds work or ambiguity without helping this use case. Requiring every caller to
implement a complete policy would preserve today's extension seam but leave the
ordinary setup problem unsolved. Provider-specific adapters continue to implement
the existing source port and use their own admitted source evidence.

Verification must cover immutable caller snapshots, namespace collisions,
selection reasons, unknown acquisition facts, exact source and schema pins,
bounded refusal and cleanup, read-only opening, equality with explicit service
assembly, and one installed example. Provider wheel qualification is separate
from the supplied-record example and must not be inferred from a synthetic input.

## Delivered scope and review

Implemented through `build_local_catalog`, `open_local_catalog`,
`SuppliedRecordSource`, and `SuppliedRecordCatalogPolicy`. The source and
architecture agents agreed on reusing existing publication and admission rather
than adding a format or a second builder. The
[independent review](2026-09-11-catalog-input-review.md) approved the resulting
scope and records the parent's executed checks separately. Source collection
outcomes, dataset-growth preview, and export convenience remain distinct work.
