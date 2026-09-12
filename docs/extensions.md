# Extend processors, execution, and storage

Start an extension at the interface that owns the behavior. Keep provider SDKs
inside adapters and connect them in composition code. The
[architecture guide](architecture.md) explains the dependency direction;
[CONTRIBUTING](../CONTRIBUTING.md#find-a-bounded-change) maps changes to tests.

## Add a processor with explicit inputs and stable identity

1. Implement the [processor interface](../src/docspec/ports/processor.py).
   [`ContentStatisticsProcessor`](../src/docspec/processing/processors.py) is a
   small working example. Its `description` declares its identity; `process()`
   receives the request, permitted payload, and prerequisite results.
2. Build a [`ProcessorDescription`](../src/docspec/domain/processors.py) with the
   implementation version, configuration, accepted inputs, output schemas,
   resource identities, cache policy, data-use policy digest, and item limits.
   Changes that affect results must change the appropriate identity inputs.
3. Add the description to the plan's processor set and register the matching
   implementation with execution. The set validates the dependency graph and
   supplies `execution_order`. Prerequisites refer to declared results for the
   same segment; they are not an unrestricted view of another task's state.
4. Read only permitted fields through the payload interface. Return the declared
   result and evidence. Let the runtime own retries, invocation receipts, and
   cumulative accounting rather than introducing a second retry loop.

External processing also needs the plan's data-use permission, declared resource
identities, and the required provider evidence. Keep credentials out of durable
requests, receipts, and errors. A processor description declares what is allowed;
the application must still validate the actual invocation and returned result.

[`processor_rules`](../src/docspec/application/processor_rules.py) supplies shared
request and result checks for new work, checkpoints, and reuse.
[`ProcessorRuntime`](../src/docspec/application/processor_runtime.py) owns
invocation, retries, and caching. Exercise
[processor behavior](../tests/conformance/test_processor_contract.py),
[data-use restrictions](../tests/test_policy_security.py), and
[reprocessing](../tests/test_processor_reprocessing.py) when changing this path.

## Replace extraction or segmentation

Implement the [extractor](../src/docspec/ports/extractor.py) or
[segmenter](../src/docspec/ports/segmenter.py) interface. An extractor exposes
`extractor_id`, `configuration_digest`, and `extract()`; a segmenter exposes
`segmenter_id`, `policy_digest`, and `segment()`. Each also implements
`selected_identity(input)`, returning the output implementation's ID and digest.
An ordinary implementation returns its own pair. A registry selects a child
using captured-file or representation metadata and shares that selection logic
with execution.

Use `docspec.runtime.stage_policy` to derive the plan's stage settings from the
actual objects, then pass those objects to `prepare_local_run`. The
[Python guide](python-runs.md#choose-extraction-and-segmentation) shows that path;
[the installed probe](../tests/support/installed_runtime_probe.py) demonstrates
small custom implementations without private application imports. Credentials
belong in live dependencies, not retained settings. Output-affecting changes,
including parser/tokenizer versions, must change the relevant digest.

Representations and extraction receipts retain the selected extractor's identity.
Segments and segmentation receipts retain the selected segmenter's identity and
policy, including an invocation that returns no segments. DocSpec checks these
values before accepting outputs and when saved entries are opened as checkpoints,
along with the existing source, byte, and evidence checks. The registry's aggregate settings remain in
the plan; they do not replace the identities of individual outputs.

## Make cache reuse conditional on verified evidence

The [cache interface](../src/docspec/ports/processor_cache.py) returns immutable
references. `put_if_absent()` may return another writer's entry, so execution
must verify that entry before accepting it. `discard()` takes the expected
reference so cleanup cannot remove a newer replacement.

Use the same request, input, resource, policy, and result checks as uncached
execution. A cache outage can cause more work; it must not make an invalid result
acceptable. [Cache tests](../tests/test_processor_cache.py) cover this boundary.

## Inject implementations through Dagster resources

[`build_dagster_definitions`](../src/docspec/adapters/dagster.py) accepts native
resource definitions. The `docspec_runtime` resource supplies a prepared run;
other resources inject its fetcher, processors, workspace and settings. Native
Dagster configuration controls execution, retries and cancellation. See the
[installed example](dagster-experiment.md) for resource construction and cleanup.

The [task model](../src/docspec/domain/execution.py) carries immutable references
and identity pins. Worker-local Python objects and document bytes stay inside
their resources. The small direct local runner uses the same task handler.

The [profile registry](../src/docspec/profile_registry.py) validates and selects
machine descriptions; it does not import or instantiate their implementations.
Command and worker composition constructs runtime adapters and application
services. Keep profile implementation strings, actual composition, and
installed-package checks aligned when moving code. Keep optional imports at the
adapter that selects them. A valid profile object alone does not demonstrate
that a deployed worker enforces its declared resource limits.

Storage-description format `2.0` retains concrete implementation settings and
limits. It removes the five placeholder governance labels: those labels never
enforced access, encryption, location, retention or redistribution. Configure
deployment controls through the actual storage implementation. DocSpec's
implemented plan data-use and retention policies remain separate.

Keep task scheduling separate from document meaning. A successful task result
identifies durable output; reconciliation still verifies it against the complete
planned population before publication. Check message portability, retries, and
local/adapter behavior with [backend tests](../tests/test_execution_backends.py).

## Implement a result sink that accounts for the full stream

The [sink interface](../src/docspec/ports/result_sink.py) accepts a store and an
iterable of delivery records. Preserve bounded iteration, record order, stable
identities, and retry behavior. Its receipt must account for the complete stream.
The current [delivery service](../src/docspec/application/delivery.py) rejects
receipts with missing, rejected, or undelivered records; partial acceptance is
not a supported completion state.

Returned references must identify the actual stored result. Repeating delivery
must preserve the same accepted meaning even if an earlier attempt wrote data
before its receipt was saved. See [sink checks](../tests/conformance/test_result_sink_contract.py)
and [delivery recovery](../tests/test_result_sinks_and_recovery.py).

## Put storage behavior behind its existing interface

Choose the interface for the data being stored: captured bytes use
[`BlobStore`](../src/docspec/ports/blob_store.py), record layers use
[record storage](../src/docspec/ports/record_storage.py), and document publication
uses the [document catalog](../src/docspec/ports/document_catalog.py).
The [local adapters](../src/docspec/adapters/storage/) keep these responsibilities
separate. Immutable writes, containment, atomic publication, and stale-base
rejection are observable behavior, not incidental file operations.

Reference validation has several depths: a type validates fields, a reader
checks referenced bytes, and a workflow verifier checks their meaning together.
An adapter must perform the checks required by its interface rather than treating
a well-formed reference as proof of valid content. Preserve incremental record
partition behavior and memory bounds. Use
[storage checks](../tests/test_storage_adapters.py),
[record/catalog checks](../tests/test_storage_records_catalog.py), and the
[maintenance guide](operations.md) for retention and publication constraints.
