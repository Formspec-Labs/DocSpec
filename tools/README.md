# Repository tools

Use the installed `docspec` command for supported application workflows. These
tools maintain generated artifacts or inspect retained samples. They are not
installed package APIs. Use [`export_local_result`](../docs/result-exports.md)
to publish a retained experiment result for an independent consumer.

Catalog policy creation now lives in the CLI:

```sh
uv run --frozen --extra dagster docspec source-catalog write-policy \
  --policy federal-register --input fields.json --output policy-member.json
```

The command uses the application policy's own parser and serializer, verifies
the round trip, and refuses an existing output. See the
[catalog guide](../docs/catalog-and-processing.md) for inputs and the complete flow.

## Find a tool by purpose

| Purpose | Files | Boundary and checks |
| --- | --- | --- |
| Measure description coverage and repeated text | [description_coverage.py](description_coverage.py), [description_prevalence.py](description_prevalence.py) | Research predicates over a retained catalog frame; `tests/test_catalog_sample_tools.py` checks distinct counts and verbatim values. |
| Draw a description sample and join docket context | [draw_docabstract_sample.py](draw_docabstract_sample.py), [draw_docabstract_companion.py](draw_docabstract_companion.py) | Preserve sampling strata, source strings, and the original receipt digest. The companion joins unique documents; its counts are not the number of sample-stratum memberships. |
| Compare catalog topics with publisher responses | [fr_topic_receipt.py](fr_topic_receipt.py) | Owns the topic strata and records live observations separately from retained catalog facts. Tests replace publisher calls with local responses. |
| Select, fetch, and summarize an attachment sample | [select_attachment_sample.py](select_attachment_sample.py), [fetch_attachment_sample.py](fetch_attachment_sample.py), [summarize_attachment_sample.py](summarize_attachment_sample.py) | Keep selection weights, acquisition receipts, quota handling, and report calculations explicit. `tests/test_attachment_sample_tools.py` covers local behavior, including interrupted receipt recovery. |
| Prove a CourtListener source population and acquisition route | [courtlistener_bulk_source.py](courtlistener_bulk_source.py) | Uses the installed SpicyDocs parser; retains DocSpec input pins, selection and coverage checks in `tests/test_courtlistener_bulk_source.py`. Development installs the reader; it remains optional for the core wheel. |
| Share retained catalog file reading | [catalog_sample_support.py](catalog_sample_support.py) | Internal helper for manifest ordering, plain/gzip JSONL rows, and receipt paths. It does not verify or admit a catalog for publication. |

Run a tool from the repository root with `uv run --frozen --extra dagster python
-m tools.<module> --help` when it provides a command. Read its input requirements
before selecting live acquisition. Focused tests use local inputs and substitutes
for publisher requests.

Keep shared product rules with their package owner. Share tool code when its
callers read the same evidence in the same way; keep each research question,
sampling rule, and receipt meaning with its tool. The result-export reader
checks retained output and source evidence. Consumer text requirements are
explicit admission choices, described with their limits in the
[export guide](../docs/result-exports.md).
