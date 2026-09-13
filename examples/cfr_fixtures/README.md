# Annual CFR example inputs

`edition.xml` and `section.xml` are authored test fixtures with invented text.
They use GovInfo MODS and annual CFR structure and realistic URL shapes. They
are not captures or legal text. The metadata offers sections 18.1 and 18.2;
only 18.1 has a body fixture. Its 2025 edition is explicitly cover-only with a
2023 original date; the annual body also states 2023. This checks date separation.

`real-section716-2.xml` is an unchanged bounded publisher capture copied from
SpicyDocs `0a26fec` (`tests/fixtures/cfr/annual-title30-vol3-sec716-2.xml`).
Its source is [2025 Title 30, Volume 3, § 716.2](https://www.govinfo.gov/content/pkg/CFR-2025-title30-vol3/xml/CFR-2025-title30-vol3-sec716-2.xml).
SHA-256: `4a6471fa7548dbd4402ae386c8c6f709907fe5be3afff90313d0802284b2cb98`.
It qualifies visible-text extraction against actual annual XML, including
inline emphasis and print-page markers. Tests read it offline; the example
never substitutes it for the selected title/section.
