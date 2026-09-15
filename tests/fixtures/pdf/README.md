# Retained PDF

`FAA-2016-6907-0001-content.pdf` is the complete 2,620-byte public Regulations.gov
attachment already retained in SpicyDocs under
`tests/fixtures/regulations_gov_attachments/`.
The source was captured unchanged on 2026-09-14 from
`https://downloads.regulations.gov/FAA-2016-6907-0001/content.pdf`.
SHA256: `f4494ea77d0f8a0ec0b6e7f64e20c6ffe6c53d3be47cd59245f42f74036a7fc0`.
This copy lets the ordinary DocSpec test suite replay real source bytes without
a sibling checkout or a live request. The independent frozen page loop lives in
`tests/support/pdf_oracle.py`.
