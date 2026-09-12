# D28: shared canonical JSON review

## 1. Summary and scope

**Static verdict: APPROVE.** DocSpec now uses the installed public Rulespec canonical JSON emitter instead of maintaining its own identity emitter and a second ASCII optimization. The change deliberately adopts the selected exact-integer range and UTF-16 key ordering. Domain conversion, contextual input errors, exact source bytes, and file framing retain their separate responsibilities.

Reviewed the current D28 source/test/document changes in `domain/identity.py`, `adapters/framing.py`, `adapters/catalog_artifact/derivation.py`, `document_release_support.py`, the corresponding encoding/framing/catalog/portable-reader tests, the installed runtime probe, static/dynamic import-boundary tests, standalone specification §4.1, `docs/canonical-json.md`, and the D28 architecture note. Concurrent retention and result-export changes are excluded. The unrelated historical findings file was not read.

This is an independent static review. The reviewer did not run tests or builds, edit production code, or commit. Parent execution evidence should be attached separately.

## 2. Function and data traces

| Path | Evidence and conclusion |
| --- | --- |
| Domain value → identity bytes | `domain/identity.py:181–251` preserves one-pass conversion of dataclasses, Enum values, mappings and sequences, including duplicate custom mapping and nested conversion errors. `canonical_json_bytes` at lines 254–257 delegates directly to `rulespec_artifacts.canonical_json_bytes`. |
| Trusted values → identity bytes | `identity.py:188–191` skips only domain conversion. Both ordinary and trusted values still reach the same shared emitter at line 257; integer and Unicode validation are not bypassed. |
| Exact canonical bytes → immutable values | `identity.py:275–285` parses closed JSON, re-emits through the shared encoder, adds the file-specific newline where required, and compares exact bytes. Shared `ValueError` becomes a labelled `IntegrityError`. Existing duplicate-key parsing remains at lines 266–271. |
| Portable JSON and JSONL | `document_release_support.py` retains its public ValueError translation and contextual file/line parsing. The duplicate safe-integer constant and recursive walk are removed because both readers already use the canonical gateway. |
| Catalog digest records | `adapters/catalog_artifact/derivation.py:77–99` emits plain record pieces directly through the shared public function. There is no alias to the deleted ASCII optimizer. |
| Several digests from one row scan | `adapters/framing.py:14–59` retains only `FramedSectionHasher`: existing count/length framing and incremental hashing, with each record encoded by the shared function. The unused batch replacement and local JSON encoder/guard are deleted. |
| Raw captured document | `tests/support/installed_runtime_probe.py:35–46` adds a large numeric literal to raw HTML, pins its exact bytes in a supplied catalog, and uses the public capture/process flow. Lines 163–167 read the retained captured blob and compare the exact source file plus the literal. No JSON conversion is applied to that document. |

The installed Rulespec 1.0.12 implementation was inspected directly: `_artifact.py:280–319` owns scalar emission, exact safe-integer bounds, Unicode validation, UTF-16 object key ordering and the public gateway. Its `ArtifactVerificationError` is a `ValueError` (`_artifact.py:176`), so DocSpec's retained parsing/error translations cover shared refusals.

## 3. Invariants and simplicity assessment

1. **One authority emits structured JSON.** All newly encoded identity and catalog record paths in scope use the same public dependency. No second scalar validator, compatibility writer, configurable encoding mode, registry, or new hierarchy is introduced.
2. **The accepted domain is explicit.** Integers are exactly within ±(2^53−1); larger integers refuse. Floats, lone surrogates, unsupported Python values and non-string keys refuse. Values are never rounded or converted to strings automatically.
3. **Identity changes are intentional.** UTF-16 sorting can differ from the former Python code-point sorting. The guide explicitly requires rebuilding affected identities from inputs rather than accepting old bytes through an alternative encoder. Valid ASCII-key records keep the same encoding.
4. **String content is preserved.** Composed and decomposed Unicode spellings remain distinct. The encoder orders keys but does not normalize document text or metadata values.
5. **Raw bytes remain raw.** A document containing a number outside the structured JSON domain remains capturable and readable. The migration does not parse and rewrite captured or upstream evidence bytes. This is separately asserted from metadata admission.
6. **The remaining helpers have a demonstrated purpose.** Domain conversion handles DocSpec Python values and error context. Incremental framing lets multiple catalog digests share a bounded scan. Neither duplicates the shared encoder's scalar/output rules. Removing both would either change supported Python inputs or require repeated reads.
7. **The dependency exception is narrow and truthful.** `tests/test_package_boundary.py:279–307` allows only `domain/identity.py` to import the exact public `rulespec_artifacts` module and asserts that exception is used. Other core third-party imports remain forbidden. `tests/conformance/test_import_directions.py:197–229` establishes the installed public encoder's actual transitive import baseline in a clean child, then refuses additional foreign imports from the complete core. The specification at lines 287–297 explicitly replaces the former standard-library-only claim; there is no lazy-import workaround or broad allowance for transport/stage implementations.

The direct adoption meets the user's DRY and simplicity direction. Preserving arbitrary integers solely for an optimizer parity fixture would have retained a competing encoder without a demonstrated product need. Conversely, adding an encoding policy abstraction would provide no value for the chosen single domain.

## 4. Test assertions inspected

- `tests/test_canonical_encoding_equivalence.py` consumes the installed public 44-case corpus. Accepted values compare exact hex bytes through ordinary, frozen, parsed and trusted paths. Rejected values also refuse under trusted conversion. Rejected byte encodings refuse at DocSpec's parsing boundary.
- The same file explicitly checks UTF-16 BMP/non-BMP key order and exactly one trailing newline, domain dataclass/Enum conversion, duplicate custom Mapping refusal, and nested conversion error paths.
- `tests/test_framing.py` compares incremental framing to the shared `framed_section_digest` with Unicode keys. Out-of-domain record values refuse before incrementing the observed count.
- `tests/test_local_catalogs.py:94–111` supplies nested metadata integers at 2^53, −2^53 and 2^63 through the ordinary supplied-record build path. Each refuses with the safe-range error, closes the source iterator, and leaves the dataset root absent.
- `tests/test_document_release_identity.py:58–71` checks portable JSON and JSONL file-context refusal plus the same emission boundary.
- `tests/support/installed_runtime_probe.py` checks shared accepted corpus bytes in the installed package, then preserves `9223372036854775808` inside raw HTML across the actual capture/retain/inspection flow.
- The deleted `test_source_catalog_rows` optimizer test tested only the removed implementation. Existing actual catalog derivation/admission tests remain; the change does not replace their identities with indiscriminate fixture restamping.

These are inspected assertions, not reviewer-executed outcomes. The parent owns the focused, installed-package, import-direction and full regression gates. No performance result is claimed by this review.

## 5. Findings and limits

No material correctness or maintainability finding remains in the stable D28 scope.

Architecture concerns were resolved before implementation: the safe-integer narrowing is an explicit selected domain; trusted conversion cannot bypass it; public metadata rejection is covered separately from raw-source preservation; and the portable readers retain their contextual errors after removing their duplicate integer walk. Parent gates exposed obsolete standard-library-only and textual ownership assertions; the reviewed narrow dependency exception fixes the intended boundary instead of masking an unexpected optional import. Both guards use the one named `SHARED_CANONICAL_GATEWAY` path and allow only the public artifact package. The renamed import test's selector is also updated at `conformance/test-matrix.json:24`. These are resolved migration changes, not remaining production defects.

Known limits are deliberate: affected old identities require rebuild, out-of-domain structured values require a producer correction, and this review does not establish large-corpus throughput. A future concrete required value outside the selected domain would need an explicit shared-domain decision, not a hidden local fallback.

## 6. Verdict

**APPROVE.** The source, tests and documentation support D28's intended simplification: one installed shared JSON emitter with necessary DocSpec conversion and framing around it. Completion still requires the parent-owned runtime gates; attach those results separately from this static assessment.

## Executed follow-up

The parent combined gate passed 138 checks, including the real installed runtime
probe and installed source integration, with one obsolete textual-boundary
assertion rejecting the gateway documentation. The exact shared-gateway
exception corrected that assertion. The follow-up package/import gate passed
13 checks; its only failure concerns the concurrently added result-export facade,
which is outside this slice and is assigned to that implementation. These runs
do not establish a new full-worktree regression result. The conformance matrix
now points to the renamed shared-dependency check.
