# Export a retained dataset

`export_local_result` copies a retained experiment result into an independently
readable dataset. It keeps the complete active population, exact document bytes,
source coordinates, dispositions, and typed processing evidence. It does not
fetch or process documents again. Export is optional: experiments can retain
capture-only results or accepted failures that a consumer would reject.

Internal experiment state remains in a `LocalWorkspace`. An exported dataset
uses the shared Rulespec `spicy-artifact/1.0` container with kind
`docspec-result-export` and product schema `urn:docspec:result-export:1.0`.
It has a separate identity from the retained result and contains no executable
workspace or run history.

## Publish from an existing experiment

Choose the content requirement and both accepted producers explicitly. The
document producer admits the existing retained result; the export producer
identifies the publisher and verifier of the new dataset.

```python
from docspec.runtime import export_local_result

# plan, workspace, and retained_ref come from the existing experiment.
export_pin = export_local_result(
    plan,
    workspace,
    retained_ref,
    destination,
    admission="nonempty-text",
    document_release_producer=document_producer,
    export_producer=export_producer,
    max_output_bytes=256 * 1024**2,
)
```

`retained-evidence` preserves valid capture-only results, complete processing,
and accounted failures. `nonempty-text` additionally requires each active
selected document to have non-whitespace UTF-8 text and no terminal failure.
The recognized text kinds are `text`, `visible-text`, and `pdf-text`; raw markup
and images do not satisfy that requirement. An empty PDF page does not make a
document empty when another page contains text. Intentional exclusions remain
in both exports.

Both choices preserve every record. A refusal raises `ExportAdmissionError`
with complete `reasonCounts`, at most ten source-qualified reasons in `sample`,
and an explicit `sampleTruncated` flag. Export never silently removes documents
to make the dataset pass. Stage completion and contradictory evidence are
integrity checks shared with experiment recovery, independently of the chosen
text requirement.

Publication is atomic and does not replace a different destination. Repeating
the same export admits the existing output and returns its pin. An exception
removes this attempt's temporary directory and publishes nothing. A killed
process can leave an unreferenced temporary directory beside the destination;
retry starts a fresh attempt. No export checkpoint or scheduler is introduced.

## Consume without the original workspace

The publisher supplies `export_pin` and its accepted `Producer` separately from
the dataset. The returned pin is the shared verifier's actual result; consumers
do not copy a textual verification claim or trust identities read from an
unadmitted directory.

```python
from contextlib import closing
from docspec.domain.content import CapturedFile, Representation
from docspec.result_export import open_result_export

with open_result_export(
    destination,
    expected_pin=export_pin,
    producer=export_producer,
    max_output_bytes=256 * 1024**2,
) as dataset:
    print(dataset.pin.as_dict())
    print(dataset.summary)

    with closing(dataset.records("representations")) as rows:
        for row in rows:
            representation = Representation.from_dict(row["payload"])
            content = dataset.read_blob(representation.blob, max_bytes=8 * 1024**2)
            print(row["sourceItemId"], representation.kind, content[:80])

    # Source coordinates refer to these retained captures, not a live URL.
    with closing(dataset.records("files")) as rows:
        for row in rows:
            captured = CapturedFile.from_dict(row["payload"])
            with dataset.open_blob(captured.blob) as stream:
                print(captured.file_id, stream.read(80))
```

`layer_kinds` lists the available layers. `records(kind)` returns unchanged
delivery rows, including their source-qualified identity and complete payload.
`read_evidence(ArtifactRef)` opens an exact stage receipt, processor result,
prerequisite result, or owning plan embedded in this export. The reader accepts
only the exact references admitted from its active records and typed evidence.
Closing a partially consumed iterator releases its file; closing the dataset
releases its disposable SQLite index.

The directory contains one JSONL file per active layer, content-addressed blobs,
the exact small control files needed to understand stage and processor results,
an index, and the shared manifest/root. Files shared by several records are
copied once. Source-catalog and prior-result pins, provider/resource identities,
and owning-plan source/base references remain external provenance. The reader
does not recursively fetch them or follow arbitrary processor JSON fields.

## What verification establishes

Rulespec verifies the exact root, accepted pin, manifest, membership, sizes and
digests. DocSpec reuses its active-layer checks for source identity, disposition
accounting, output identities, coordinates and relations. Shared receipt checks
bind successful output and failed attempts to their retained item and owning
processor plan, including inherited work from an older plan.

The reader checks segment bytes against their exact representation slices and
checks every declared identity mapping against captured bytes. Named derived
transformations, including HTML/XML visible-text and PDF extraction, remain
recorded evidence; the reader does not instantiate their parsers to replay them.
`derivedMappingsNotReplayed` makes that distinction visible. Declared mapping
checks do not prove that all meaningful source content was extracted.

The result-specific export identity pins the retained artifact's physical
digest in its small product specification. This distinguishes same-plan results
with different failures or outputs, while Rulespec continues to derive both
export identities. Pinned blob and control bytes are copied exactly; the shared
canonical JSON domain governs newly emitted record and index values.

## Bounds and text suitability

The required `max_output_bytes` bounds the entire artifact, including root and
manifest. The reader checks manifest-declared totals before reading payloads.
Metadata/index roots are limited to 1 MiB, JSONL rows and controls to 8 MiB,
and one item's metadata and typed control dependencies to 64 MiB. Disposable
index input is bounded at four times the total artifact allowance.

The first reader verifies representation/segment bytes with the existing
in-memory evidence primitives, so each representation, segment and captured
source needed for an identity mapping must fit 64 MiB. Larger capture-only
blobs stream within the artifact bound. `read_blob` also requires the caller's
own memory allowance; `open_blob` provides a verified seekable stream.

The text observations distinguish nonempty, empty and invalid UTF-8 output.
They are content facts, not a semantic quality score. Nonempty text can still
omit an important passage, contain boilerplate, or faithfully represent an
already incomplete upstream source. Expected source digests/sizes detect a
mismatch with pinned input; they cannot establish that an unpinned upstream
document was complete. See [representation choices](representations.md) for
format-specific limitations and source evidence.

The normal export path does not use campaign-specific portable builders or
retention fractions. A visible-text-to-markup byte ratio measures a
representation choice: suppressing long scripts/tags can make complete useful
text very small, while duplicated boilerplate can make misleading text large.
Those ratios do not admit these exports. The superseded campaign builder and
format are scheduled for deletion after this export's qualification. Historical
reproduction is not a support requirement; this API supplies no legacy import
or compatibility shim.

The [quality challenge tests](../tests/test_extraction_quality.py) make these
limits concrete: a complete visible parse can retain under 1% of its markup,
while valid declared mappings and over 90% retention can still omit a critical
block. A named parser resolver detects invented block text or a truncated page
claim; an independent consumer does not load that resolver. The PDF challenge
injects deterministic provider pages to test DocSpec's evidence behavior, rather
than qualifying a real PDF parser. Empty pages remain explicit observations.
