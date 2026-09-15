# Locally qualified dependencies

The required SpicyDocs core reader and its source-reading, bill-acquisition,
annual-CFR and FEC metadata tests use one wheel. `spicy_docs.json` records its
version, source revision and SHA-256; `pyproject.toml` and `uv.lock` select that
same file. These dependency wheels are not bundled inside DocSpec's built wheel.

When accepting a new SpicyDocs release, replace the wheel and manifest, update
the package pin and lock, and run the source-catalog, GovInfo bill/CFR, FEC and
package boundary tests. Retain acquisition as an optional provider extra. See
[installation](../CONTRIBUTING.md#install-the-source-reader) and the
[bill](../docs/govinfo-bill-example.md), [annual CFR](../docs/govinfo-cfr-example.md)
and [FEC metadata](../docs/fec-committees.md) examples.

SpicyDocs shares its ordered XML tree between MODS and PREMIS. The annual
CFR example imports `XmlTreeElement` from that owner and retains `ModsRecord`
field selection, exact XML bytes and source positions. Its installed-provider
fingerprint changes with the new wheel; stored catalog data is not regenerated.

The `pdf` extra selects the shared pypdf page reader. PDF stage configuration
names the installed reader version and module hash alongside the pypdf version.

JSON values and record positions, XML/HTML events and image headers use the same core
wheel. DocSpec retains normalization, headings, representation and evidence
policy. Stage settings bind the selected installed source files before parsing.
