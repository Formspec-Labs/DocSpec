# Retained markup cases

Copied unchanged from the SpicyDocs source fixtures for PAR13 receiver tests.
These small cases qualify parsing/layout; they do not establish source coverage.

| File | Provenance | SHA-256 |
| --- | --- | --- |
| `annual-title1-edition.xml` | GovInfo 2025 Title 1 volume 1 MODS, captured 2026-09-12. Reserialized selection of root extension, originInfo and titleInfo; nested constituents omitted. | `51af27ca7bd05393d27445704a46b925aecf01cf3ff687b26fdac9c268e7c9b3` |
| `subject-index-45.html` | Exact retained National Archives HTML bytes `[102153,102551)`, captured 2026-08-20. Includes the publisher's malformed `ddgrant` tag. | `0c330f9fe3e7e1c441f6b52cecc1d17984be76b073b2ae692b8c068af462d795` |

The MODS source was
`https://www.govinfo.gov/metadata/pkg/CFR-2025-title1-vol1/mods.xml`;
full capture: 1,342,199 bytes, SHA-256
`6ae66a2ba6939c1c0c307199dcab3ba69aa1e607f3a4640dc58ba17b2aa4ceed`.
The excerpt's positions address the fixture, not that full capture.

The HTML source was
`https://www.archives.gov/federal-register/cfr/subject-title-45.html`;
full capture SHA-256
`178f0f791aae7f85c7c0c853ba7529e60ac00cde514cd1c1e55dbd8a100f26e3`.
The original provider provenance is in
`tests/fixtures/cfr/README.md` and
`tests/fixtures/cfr_metadata/roster-index-provenance.json` in SpicyDocs.
