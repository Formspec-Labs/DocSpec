# Configured extraction and segmentation

Decision: keep stage configuration in the existing processing plan and derive
it from the objects supplied by a Python caller. The solutions architect
recommended this approach after inspecting planning, execution, checkpoint
recovery, default registries, optional PDF support, and current callers. The
parent accepted it for D13. Validation of the implementation is recorded
separately; this document records the judgment and scope.

The plan names one effective extractor, its configuration digest, one effective
segmenter, and its policy digest. An effective stage may be a registry. This
replaces the extractor-name tuple, which did not describe the one extractor
object actually injected into execution. `docspec.runtime.stage_policy` derives
these values; domain code does not import the runtime or implementations.

Each stage exposes one `selected_identity(input)` method. Ordinary leaves return
their own ID and digest; the PDF adapter names its pinned parser output. A
registry shares selection logic between this method and
execution, and hashes its routing plus actual child settings into its aggregate
digest. Output records retain child identities; an empty segmentation receipt
also records its selected child and policy. The shared application checks bind
new outputs and recovered evidence to those declared choices.

This makes a contributor's changed settings visible to the planner and prevents
silent reuse under an unchanged registry name. Existing plan, entry, and store
identities carry the change, and the existing full-repair path rebuilds affected
documents. Reusing captures after extraction changes or representations after
segmentation changes is D15, not a claim of this decision.

Two alternatives were rejected. Checking only worker configuration would miss
planner decisions that produce no tasks. Adding a descriptor hierarchy or plugin
loader would add extension work without helping this explicit Python use case.
The selected interface checks declared configuration and output consistency;
implementers still own arbitrary plugin correctness and complete declarations of
output-affecting settings.

PDF configuration reads installed distribution metadata without importing the
parser. Its exact version, availability, and settings affect the default registry
pin. A selected parser must match that pin before processing. This conservatively
invalidates text-only plans using the default registry when PDF availability
changes; an explicitly selected text extractor avoids that choice. The tradeoff
favors clear, safe invalidation before adding media-specific reuse.

Processing plans, document stores, entry schema identifiers, and ordinary
segmentation receipts adopt the new closed shapes together. No legacy reader is
kept. The unchanged storage adapter still uses its existing backend/profile
identity; the checked profile description digest changes with its schema list.
Source evidence and retained datasets are not deleted by this format change.

Narrow searches found no direct stage-API callers in current SpicyDocs,
SpicySearch, or SpicyEngine Python source. No sibling migration task was inferred
from historical probes. Future consumer adoption remains in each destination
repository's own plan.
