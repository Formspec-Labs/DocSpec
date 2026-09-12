# D28: one canonical JSON emitter

The Rulespec RS01 decision selected its existing safe-integer and UTF-16 JSON
domain. The installed `rulespec_artifacts` 1.0.12 public encoder and corpus
implement that decision. DocSpec had no demonstrated requirement for arbitrary
integers: the concrete `2**63` case was an optimizer parity fixture. Keeping a
second emitter to preserve that fixture would add maintenance without a stated
user requirement.

DocSpec now delegates both domain identity encoding and catalog record encoding
to that public function. Its one-pass domain conversion retains contextual
errors and converts dataclasses, Enum values and immutable containers. Shared
encoding still validates every scalar when a caller uses the existing trusted
conversion context. Canonical readers keep their own contextual refusal and
newline rules.

The ASCII guard, stdlib record emitter, unused batch framing replacement and
redundant portable safe-integer walk were deleted. The incremental framer stays:
real catalog derivation uses it to compute several digests in one bounded scan.
There is no second scalar validator, compatibility writer, registry or configurable
encoding mode.

The old standard-library-only core assertion changes deliberately: the domain
identity gateway may import the public Rulespec package. Static checks permit
that single module edge; a clean-interpreter test admits the public package's
actual transitive dependencies and rejects additional foreign core imports.
This does not admit concrete storage, transports, parsers or processors into
domain/application modules.

The deliberate identity change and the boundary between structured JSON and
exact raw source bytes are documented in [canonical JSON](../canonical-json.md).
Existing valid ASCII-key schemas and fixtures should retain their pins; existing
catalog, portable-release and installed-wheel tests verify that assertion.
Fixtures must not be indiscriminately resealed to hide a changed identity.

Acceptance evidence consists of the installed shared 44-case corpus, contextual
domain conversion, real catalog framing and reader tests, public supplied-record
refusal before publication, portable JSON/JSONL refusal, and an installed raw
capture containing an integer outside the structured JSON domain. Root owns
test execution and records the final result separately. Independent architecture
review approved the decision before implementation. No sibling files changed.
