# Select a retained GAO topic

This example carries a publisher's literal topic from retained HTML into a
DocSpec catalog, then filters that catalog by an exact label. SpicyDocs reads and
validates the source page. DocSpec preserves its fields and evidence and applies
the caller's selection. A topic label establishes no legal requirement or
applicability.

From a checkout, run each case into a new absolute output directory:

```sh
uv run --frozen python -m examples.gao_topics --case matching --output /tmp/gao-topic-matching
uv run --frozen python -m examples.gao_topics --case unexpected --output /tmp/gao-topic-unexpected
uv run --frozen python -m examples.gao_topics --case missing --output /tmp/gao-topic-missing
```

The default label is `Information Security`. Supply `--topic 'Agency Operations'`
to match the unexpected-label fixture instead. Comparison is case-sensitive.
These small [inputs](../examples/gao_fixtures/README.md) are unchanged synthetic
SpicyDocs fixtures; the example makes no live requests. It needs the optional
SpicyDocs reader, included in checkout development dependencies, and requires
neither Dagster nor provider acquisition/analytics extras.

| Case | Source result | DocSpec result |
| --- | --- | --- |
| Matching | Retains `Information Security` and the original HTML. | One catalog record matches the default filter. |
| Unexpected | Retains `Agency Operations` unchanged. | The record remains in the catalog; the default filter does not match it. |
| Missing | Refuses the absent publisher topic field and retains the response. A navigation link does not count. | No catalog is built; the failure remains inspectable. |

Inspect `gao-topics.json` for the source outcome, catalog reference and topic
selection. `source-result.json` is the provider command's unchanged report.
The `source/` release and `source-blobs/` directory retain exact accepted evidence;
on refusal, the report points to the retained response in the blob directory.
The `dataset/` workspace holds the ordinary DocSpec catalog.

Each catalog row preserves the admitted provider record, source description and
record-evidence reference in `sourceNativeFacts[].fields.metadata`. The topic's
origin is `record.publisherTopic`; the retained HTML digest connects that field
to the original page. The catalog is a caller-supplied mapping of admitted source
records, and its metadata retains that original source pin and collection outcome.
It does not claim to be a new native source release.

The provider offers no report attachment rendition here. Catalog records remain
unavailable for document capture, while their metadata can still match the topic
filter. Changing a filter neither captures documents nor edits the catalog.

For another retained GAO release, open it with `SpicyDocsSourceNativeAdapter`,
the public GAO profile, its exact artifact pin and your independently accepted
source verifier. Pass that reader and a `LocalWorkspace` to
[`build_topic_catalog`](../examples/gao_topics.py); inspect or filter the result
through `open_local_catalog` and `topic_selection`. The example bounds its mapping
to ten records and 1 MiB of supplied metadata. General dataset needs can use the
[same public catalog APIs](catalog-inputs.md) with explicit limits.

The [behavior test](../tests/test_gao_topic_example.py) checks exact topics, original
HTML, accepted pins, retained refusal bytes and repeated read-only filters. The
installed-provider gate runs that same test outside the checkout, before adding
HTTP support, and exercises the command with an explicitly chosen label.
