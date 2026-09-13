# DocSpec Core Model
### A PROV-Based Application Profile

*Independent Editor’s Draft. This document is not a W3C publication.*

## Abstract

DocSpec defines an application profile of W3C PROV for constructing, capturing, transforming, and revising datasets. It adds requirements for identifiable dataset membership, retained data, operation descriptions, declared dependencies, and policy-controlled reuse.

The model supports arbitrary root values, non-destructive dataset revision, independently referenceable results, and selective reuse without prescribing storage, serialization, orchestration, or execution mechanisms.

## 1. Scope and Conformance

Capitalized requirement terms, including **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**, carry the requirement levels established by BCP 14, comprising RFC 2119 and RFC 8174. Lowercase uses retain their ordinary meanings.

Sections 1–8 define **DocSpec Core**. Section 9 defines the optional **Keyed-State Profile**. Appendices are informative.

A conforming Core implementation MUST satisfy Sections 1–8. An implementation claiming Keyed-State Profile conformance MUST additionally satisfy Section 9.

An implementation MUST preserve sufficient information to recover the identities, distinctions, and relationships required by its conformance claim. Concepts MAY share physical records or representations. A separate object, identifier, or transaction for every conceptual distinction is not required.

Conformance does not require execution functionality. An implementation MAY operate exclusively on previously retained data and provenance.

DocSpec does not prescribe an application-level root schema, serialization, transport, query language, storage technology, orchestration model, scheduling, retry behavior, hashing scheme, or correspondence algorithm. Core conformance does not guarantee deterministic re-execution or complete preservation of an execution environment.

## 2. PROV Foundation

DocSpec adopts the meanings established by **PROV-DM** and the applicable consistency requirements of **PROV-CONSTRAINTS**. Its domain-specific concepts extend those meanings rather than redefine them. The recoverable PROV interpretation of a conforming description MUST satisfy those requirements.

The following bindings are normative:

| DocSpec concept | PROV foundation |
|---|---|
| Artifact or root-record occurrence | Entity |
| Dataset state | Collection |
| Particular operation execution | Activity |
| Capture or transformation execution | Activity, distinguished by DocSpec type |
| Production of a new entity | Generation |
| Actual utilization of an entity | Usage |
| Established entity-to-entity dependence | Derivation |
| Dataset membership | Membership |
| Operation definition, when separately identified | Entity, as the Plan of an Association |

These bindings use the types and relationships defined by PROV-DM.

A transformation execution is an Activity, not a PROV derivation relationship. Input bindings, declared dependencies, capture origins, result bindings, reuse associations, and retention status carry the additional semantics established by DocSpec.

DocSpec-specific information MUST NOT automatically imply PROV usage, generation, derivation, or another relationship unless that relationship is established.

Implementations MAY recover the required description from ordinary records, tables, files, or other representations. Conformance does not require RDF, a graph database, a particular provenance serialization, or a runtime inference engine.

## 3. Dataset States and Occurrences

### 3.1. Root Dataset States

A **dataset state** is an identifiable collection with recoverable membership and relevant structural properties.

Dataset construction begins with a **root dataset state** containing **root-record occurrences**. Each occurrence represents a particular member whose value MAY be scalar, structured, an identifier, a location, or other application-defined data.

Occurrence identity MUST be distinguishable from value equivalence. Two occurrences MAY represent identical values while remaining distinct members. References MUST distinguish the intended occurrence and its membership context.

A changed value MUST NOT be assigned to an earlier retained occurrence entity as though that entity were unchanged. An application-level key MAY remain stable across revisions while identifying different occurrence entities or retained versions.

Identifying an occurrence or state does not, by itself, assert that its complete content is retained. Retention claims are governed by this section and Section 5.

### 3.2. Complete Retained States

A state represented as retained MUST preserve sufficient information to recover its complete effective membership, the member values or references specified by that state, and its relevant structural properties.

Ordering, multiplicity, keys, and other structural properties MUST be preserved where they affect interpretation or declared operations. Implementations MUST NOT discard relevant distinctions by assuming set semantics.

Completeness MAY be established through a retained manifest, table, collection, composition, or another representation. It does not require a separate provenance statement or physical record for every member.

Retaining selected projections does not establish retention of the complete parent state. In particular, a retained root state MUST preserve the root-record values it claims to contain, not only the fields used by a particular operation.

### 3.3. Revision and Composition

A revised state MAY add, remove, replace, reorder, select, join, or otherwise compose members without modifying an earlier retained state.

Composition affecting particular members MUST identify those occurrences or apply a defined addressing rule whose effect is unambiguous. Where composition order affects the result, that order MUST be recoverable.

A state MAY be represented directly or through retained inputs and a defined composition rule. Deferred resolution MUST recover the particular retained state rather than produce a different state because an external source, rule, or resource changed.

Dataset construction and composition use the operation model in Section 4 when represented as operations. A separate dataset-definition class or execution model is not required.

Imported states need not include invented accounts of construction activities whose provenance is unknown.

## 4. Operations and Provenance

### 4.1. Definitions, Inputs, Executions, and Results

An **operation definition** describes the procedure requested. It MUST identify its implementation and implementation version, together with configuration, resources, and other properties material to distinguishing the operation. A property is **material** when a difference in it could change an operation’s outcome or the interpretation of its result. An operation definition MAY be recorded inline or separately.

An **input binding** identifies particular data supplied at the represented operation boundary. It MUST distinguish a whole value or dataset state from a projection. Input bindings establish data-retention obligations under Section 5; declared dependencies establish the basis for correspondence under Sections 6–7.

An **operation execution** is a particular attempt to apply an operation definition to inputs. An **operation result** records its outcome and identifies any outputs. Multiple executions of the same definition against corresponding inputs MAY produce identical or different results.

What a successfully retained result MUST identify and retain is specified in Section 5.2.

### 4.2. Capture

A **capture execution** associates a root-record occurrence with artifacts serving as **raw data** in the relevant dataset-construction context.

Capture MAY consist of retrieval, generation, import, observation, identity transformation, or adoption of an existing retained artifact. Each retained capture result MUST identify its originating occurrence and operation definition.

An external system observed during capture need not itself be retained. The captured output, bound data inputs, source-identifying information, and operation description are subject to Section 5.

The role *raw* does not assert that an artifact contains unmodified publisher bytes or that no processing occurred before its creation.

### 4.3. Transformation

A **transformation execution** consumes artifacts, root-record occurrences, dataset states, projections, or combinations thereof and yields artifacts or states serving as **derived data** in that context.

Results MAY become inputs to subsequent operations. Dataset composition, document processing, and collection-wide computation use the same execution-provenance model.

Generation dependencies among retained results MUST be acyclic. An entity MUST NOT depend on itself for its generation. Iterative computation MAY be represented through distinct entities for successive iterations.

### 4.4. Result Bindings and Contextual Roles

A **result binding** identifies an artifact or state returned by an execution, its role in that context, and whether it was newly generated or already existed.

Raw and derived are contextual roles, not globally disjoint artifact identities. An artifact derived in one context MAY be adopted as raw input in another without changing its original provenance.

Returning, adopting, or referencing an existing entity MUST NOT be represented as generating that entity again. A genuinely new representation MAY be described as a new entity with appropriate provenance.

This distinction is consistent with PROV’s separate treatment of entities, activities, generation, and participation roles.

### 4.5. Actual Provenance and Dependency Declarations

Actual utilization and production MUST remain distinguishable from conservative dependency declarations.

Established utilization and production are represented using PROV usage and generation. A declaration alone MUST NOT be interpreted as evidence that an execution actually utilized an entity.

Likewise, use of an input and production of an output do not alone establish that the output was derived from that input. Derivation assertions MUST describe an established relationship.

Implementations need not instrument every internal field access. Provenance MAY be recorded at an appropriate operation or batch boundary, provided that it does not assert unsupported relationships.

## 5. Retention and Successful Outcomes

### 5.1. Meaning of Retention

An artifact is **retained** when its particular logical value or content remains independently obtainable and its required relationships remain recoverable.

Identity is distinct from content equivalence. Distinct observations or results MAY have identical content without becoming the same provenance entity.

A mutable location alone is insufficient unless the implementation can recover the particular value associated with the retained reference. Data MAY be represented inline, by immutable storage reference, by object version, by content address, through indirection, or by another mechanism.

Retention requirements attach to specific claims about data, states, results, and associations. A provenance reference alone does not assert retention of the referenced entity’s complete content.

### 5.2. Successful-Retention Contract

An operation result MUST NOT be represented as **successfully retained** until the following information is retained and referenceable:

| Component | Retention requirement |
|---|---|
| **Whole-value data input** | The particular value or content identified by the input binding. |
| **Dataset-state input** | The complete state defined by the binding, satisfying Section 3.2. |
| **Projected data input** | The projection definition and parameters, its particular resulting value, and sufficient origin information to identify the parent entity and relevant occurrence or membership context. |
| **Operation description** | Implementation identity and version, effective material configuration values, and material resource-identifying information as specified below. |
| **Outcome and outputs** | The recorded outcome and each output represented as retained, including explicit empty or null outcomes where applicable. |
| **Provenance** | The execution identity, input and result bindings, declared-dependency descriptions, capture origin where applicable, and required execution relationships. |

A projected value MAY be retained directly or recovered from retained parent data and a projection definition whose resolution yields that same value. A separate physical copy is not required.

Retaining a projection does not require retaining the parent’s entire value or enclosing state solely because they identify its origin. Conversely, a narrower dependency declaration does not reduce the retention obligations of an input bound as a whole value or state. Any separate claim that the parent or enclosing state is retained remains subject to its full retention requirements.

For implementations and material resources, Core requires identifying information, relevant versions or descriptions, and effective material configuration values. It does not automatically require archival of executable binaries, model weights, external databases, or the complete execution environment. A resource explicitly bound as a data input is subject to the corresponding data-retention requirement.

Known uncertainty about resource identity or version MUST remain distinguishable from an established identity or version. A recorded resource description is not, by itself, evidence sufficient for every correspondence decision.

Retention of one result does not automatically require retention of the complete content of every ancestor reachable through provenance links. Explicit data-input, dataset-state, and profile-specific retention obligations remain binding.

### 5.3. Status and Availability

Execution completion, successful retention, current availability, and reuse eligibility are distinct properties.

Failed, interrupted, or incomplete outcomes MUST remain distinguishable from successfully retained outcomes containing empty or null results. Absence of an output alone MUST NOT be interpreted as success or failure.

A historical successful-retention status MUST NOT be used as proof that all required data remain currently available. Where provenance survives deletion, that historical status and current availability MUST remain distinguishable.

### 5.4. Execution and Persistence

Operations MAY consume data in memory or through streaming before persistence completes. Computation and persistence MAY overlap.

The recorded entities and relationships MUST respect PROV’s generation and usage constraints. In particular, a description MUST NOT assert usage of a completed artifact before that artifact existed. Partial-stream processing MUST use entities or operation boundaries that accurately describe what was available and utilized.

Successful retention does not require an intervening storage read, a separate commit for each result, or a persistence barrier before downstream computation.

### 5.5. Operation Boundaries

An operation MAY contain multiple internal computational steps. Transient values need not become retained DocSpec artifacts unless they are declared as retained outputs or bound as data inputs to separately represented operations.

Implementations MAY fuse separately represented operations, but fusion MUST NOT waive retention or provenance requirements for their bound data inputs and outputs.

A transformation performed during capture MUST preserve its reference to the corresponding retained raw data when its result is represented as successfully retained.

### 5.6. Historical Preservation

Creating a new state, definition, execution, result, or reuse association MUST NOT implicitly overwrite, delete, or render inaccessible an earlier retained state or result.

A new state or result MAY supersede an earlier one as preferred or current without removing it. Retained data MUST NOT be removed except under an explicit deletion or retention policy.

Discovering an error in an operation, dependency declaration, or result does not itself authorize deletion of its historical record.

## 6. Declared Dependencies and Projections

### 6.1. Dependency Descriptions

A **declared dependency** identifies application data, configuration, or a material resource relevant to establishing correspondence for an operation.

Declarations MAY conservatively include more than the minimum dependency set. They MAY identify complete inputs, dataset states, resource descriptions, or defined **projections**.

A projection identifies a field, property, subset, portion, or other application-defined view. Its definition and parameters affecting interpretation MUST be recoverable. Membership, ordering, keys, or absence MUST be accounted for where they affect the projection.

A provenance association does not automatically make every property of an associated entity a dependency. Identifying a capture’s originating occurrence does not require its reuse decision to depend on every field of that occurrence or on the entire enclosing state.

DocSpec does not prescribe a projection language or require automatic discovery of the smallest possible dependency set.

### 6.2. Adequacy for Correspondence

The operation definition, dependency descriptions, and evidence used to establish correspondence MUST adequately account for the material application inputs and resources necessary to justify that decision.

An implementation MUST NOT treat a material difference as irrelevant solely because the corresponding dependency was omitted from a declaration.

Adequacy MAY be established through explicit operation contracts, supplied declarations, analysis, retained evidence, or other mechanisms. Core does not require exhaustive instrumentation or proof of every possible dependency of arbitrary code.

A conservative declaration MAY reduce reuse opportunities without violating conformance.

### 6.3. Discovered Omissions

When an implementation discovers a material omission, it MUST NOT continue to establish reuse eligibility through a justification that relies on ignoring that omission.

The discovered omission MUST remain recoverable with the affected dependency descriptions or results. Correspondence MAY subsequently be established through corrected descriptions or adequate supplemental evidence.

Supplementation MUST preserve the original record and distinguish added information from information recorded for the original execution. Unknown historical inputs MUST NOT be fabricated or inferred solely from current values.

A dependency omission does not, by itself, require deletion of outputs, conversion of a historically retained execution into a failed execution, or re-execution of unrelated work. It affects the correspondence decisions that rely on the incomplete information.

### 6.4. External Observation and Nondeterminism

Dependency adequacy does not require deterministic replay or preservation of the entire external world.

An operation MAY observe an identified external source or permit nondeterministic behavior. Freshness, renewed observation, and acceptable external variability remain subject to reuse policy.

Explicit application inputs and material resource differences MUST NOT be omitted merely because an operation is nondeterministic or observes external systems.

## 7. Correspondence, Reuse, and Dataset Associations

### 7.1. Eligibility

An implementation MAY reuse a retained result when it can establish that the result **corresponds** to the requested operation definition and declared dependencies, subject to Section 6.

Correspondence establishes **reuse eligibility**. It neither requires reuse nor implies that a new execution would produce an identical result.

For projected dependencies, correspondence MUST account for the projection’s meaning and relevant value or identity, together with the operation definition and other material dependencies.

Where the declared dependencies are adequate under Section 6.2, a change outside them MUST NOT, by itself, make a result ineligible. A change to an enclosing dataset-state identity does not by itself prevent correspondence unless that state or its relevant structural properties are dependencies.

### 7.2. Reuse Policy

A **reuse policy** MAY reject an eligible result based on freshness, nondeterminism, external state, resource state, age, cost, user instruction, or a requirement for a new execution or observation.

A new execution MAY coexist with eligible prior results. Eligibility and policy acceptance do not establish current data availability (Section 5.3).

### 7.3. Recoverable Reuse Associations

An **operation request** identifies the requested operation definition and input bindings. It need not be a separately stored object.

When reuse associates a result with an occurrence or dataset state, the retained association MUST identify:

| Element | Required information |
|---|---|
| **Target context** | The occurrence or dataset state for which the result was selected, including relevant membership context. |
| **Request** | The requested operation definition and input bindings, including applicable projection and dependency information. |
| **Selected result** | The existing operation result and the outputs selected to satisfy the request. |

The association MUST recover the result actually selected. A rule that merely selects any currently eligible result is insufficient to preserve an earlier selection when multiple alternatives may exist.

For an association represented as retained, its requested data-input bindings, selected outputs, original operation provenance, and association information MUST satisfy Section 5 as applicable.

The association MAY be encoded in a manifest, table, shared mapping, or another representation. A separate reuse event, per-record transaction, or decision log is not required. Additional policy rationale and decision evidence MAY be recorded.

### 7.4. Preservation of Original Provenance

Reuse MUST preserve the provenance of the original execution and result. A new association MUST NOT rewrite the original inputs, originating occurrence, or producing execution.

Satisfying a request through reuse MUST NOT imply that the requested producing operation executed again. The new association describes why an existing result participates in the new context; it does not replace the account of how that result originated.

### 7.5. Implementation Freedom

Correspondence MAY be established using retained identities, versions, change records, indexes, comparisons, dependency analysis, or other mechanisms.

Conformance does not require a complete dataset scan, reconstruction, or content rehash for each decision. Incremental execution is optional.

An implementation MAY reuse unaffected results, execute operations again, or employ operation-specific incremental algorithms, subject to the requirements above.

## 8. Logical and Physical Representation

DocSpec identities, relationships, and retention requirements are logical. Independently referenceable entities MAY share files, tables, batches, or other physical representations.

Implementations MAY batch or vectorize execution, group persistence and metadata updates, and store provenance and reuse associations through shared definitions, manifests, or indexed relationships. Required associations MUST remain recoverable at the granularity represented.

Logical artifacts, executions, results, and associations do not require separate physical objects, tasks, database rows, or transactions.

Implementations MAY deduplicate storage without collapsing required identities or provenance. They MAY compact storage, materialize checkpoints, or replace physical representations without creating new logical states, provided that the values, identities, references, and required relationships of all still-retained states and artifacts remain recoverable.

A retained composition history does not require replaying every historical operation whenever a state is accessed.

## 9. Keyed-State Profile

### 9.1. Foundation

This optional profile incorporates the dictionary semantics of **PROV-Dictionary: Modeling Provenance for Dictionary Data Structures**, a W3C Working Group Note dated 30 April 2013. Implementations claiming this profile MUST satisfy its applicable semantics and constraints in addition to DocSpec Core.

A keyed state is a PROV Dictionary. Membership and applicable revisions use `prov:hadDictionaryMember`, `prov:derivedByInsertionFrom`, and `prov:derivedByRemovalFrom`.

An insertion at an existing key changes the association in the resulting dictionary, not in the earlier dictionary. Insertion and removal relations describe the complete specified membership changes between their endpoints.

### 9.2. Occurrence Addressing

Keys address members within a state; they need not be fields in root-record values.

For root datasets, associations MUST preserve distinguishable occurrences. Equal values at different keys MUST NOT be collapsed into one occurrence.

A key MAY identify a replacement occurrence in a later state. Its earlier association MUST remain recoverable while the earlier state remains retained.

Ordering is not implied by keys. An ordering interpretation MUST be preserved separately where material.

### 9.3. Revision and Completeness

An implementation MUST NOT assert insertion or removal relations while omitting additional membership changes between the same endpoints. Composite edits MAY use an ordered sequence of relations or general transformation provenance.

Conflicting revisions of one state MUST have a defined application order or be rejected as ambiguous.

A retained keyed state MUST satisfy Section 3.2. Dictionary membership assertions alone do not establish complete membership.

This profile does not require separate overlay files, physical copies of intermediate states, or replay of the complete revision history.

## Appendix A. Projection, Retention, and Reuse Example — Informative

The following illustrates the model without prescribing a serialization.

```text
State S1:
    B → occurrence B1
        url: document-location
        language: en

State S2:
    B → occurrence B2
        url: document-location
        language: fr
```

The retained overlay replaces `language`; `S1` remains unchanged.

Suppose the operation boundaries and dependencies are:

```text
capture:
    data input = occurrence.url
    output     = captured artifact

parse:
    data input = captured artifact
    output     = parsed text

translate:
    data inputs = parsed text, occurrence.language
    output      = translated text
```

For the capture, successful retention requires the projected URL value, projection description, origin information, operation description, captured artifact, and required provenance. The URL may be stored directly or recovered from a retained parent.

That capture does not independently require the entire occurrence or dataset state. However, claiming that `S1` is retained requires preserving its complete root values and membership.

If the operation definitions, other dependencies, and policy permit reuse, the new context can select the earlier capture result:

```text
Target:
    S2 / B2

Request:
    capture definition
    input = B2.url

Selected result:
    C1, originally executed for B1
    output = captured artifact A
```

This association is retained. C1’s original provenance remains unchanged.

If translation used `language` but declared only `text`, the omission cannot justify reuse across the language change. Adequate historical evidence might repair the correspondence assessment; otherwise, eligibility has not been established. Existing outputs remain historical results.

## Appendix B. Document-Capture Preservation — Informative

Core’s raw-data role does not guarantee preservation of an original document representation. A generated value, imported table, or previously derived artifact can legitimately serve as raw input.

A separate document-capture profile can impose a stronger contract: independently retain the source payload observed at a declared acquisition boundary, alongside any derived text or other representations.

Such a profile would specify which representation is preserved and identify decoding, rendering, extraction, or other processing that changes it. A claim to preserve captured source bytes would not be satisfied solely by retaining extracted text.

This draft does not define a document-capture conformance class. The distinction leaves Core general without treating its raw-data designation as a substitute for an explicit source-preservation guarantee.

## Appendix C. Additional Bindings — Informative

**Catalog publication.** A binding can build on DCAT 3 for published datasets, distributions, and version relationships. It need not make every intermediate artifact a catalog entry.

**Research exchange.** A binding can build on RO-Crate and Process Run Crate for computational tools, executions, inputs, and outputs, while additionally preserving DocSpec’s dependency, retention, state-completeness, and reuse-association information.

**JSON addressing and overlays.** A JSON binding can use JSON Pointer and JSON Patch, supplemented by retained base-state references, occurrence-addressing rules, and non-destructive application semantics. These formats would not constrain non-JSON root values or general projections.

These are interoperability directions, not additional conformance classes defined by this draft.

## References

### Normative References

| Reference | Publication |
|---|---|
| **BCP 14** | RFC 2119, *Key words for use in RFCs to Indicate Requirement Levels*, March 1997; RFC 8174, *Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words*, May 2017.  |
| **PROV-DM** | W3C, *PROV-DM: The PROV Data Model*, Recommendation, 30 April 2013.  |
| **PROV-CONSTRAINTS** | W3C, *Constraints of the PROV Data Model*, Recommendation, 30 April 2013.  |
| **PROV-DICTIONARY** | W3C, *PROV-Dictionary: Modeling Provenance for Dictionary Data Structures*, Working Group Note, 30 April 2013. Normative only for the Keyed-State Profile.  |

### Informative References

PROV-O, *The PROV Ontology*; DCAT 3, *Data Catalog Vocabulary—Version 3*; Process Run Crate; RFC 6901, *JavaScript Object Notation (JSON) Pointer*; and RFC 6902, *JavaScript Object Notation (JSON) Patch*.
