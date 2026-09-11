"""Regulations.gov policy configuration and ordered, resumable catalog construction."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from docspec.application.catalog_policy import (
    http_url as _http_url,
    utf16_key as _utf16_key,
)
from docspec.domain.identity import canonical_json_bytes, closed_mapping, sha256_digest
from docspec.domain.source_catalog import (
    CatalogDisposition,
    SourceCatalogItem,
)
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    FRESH_BUILD,
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    SourceInputSelector,
    SourceRecordCollisionResolution,
)

from .documents import (
    _item_from_row,
)
from .indexed_rows import (
    _FEDERAL_REGISTER_INDEX,
    _FEDERAL_REGISTER_KEY_PATH,
    _index_rows,
    _stage_universe,
)
from .records import (
    _COMMENT_SCHEMA,
    _COMMENT_SCOPE,
    _DOCKET_SCHEMA,
    _DOCKET_SCOPE,
    _DOCUMENT_SCHEMA,
    _DOCUMENT_SCOPE,
    _FEDERAL_REGISTER_SCHEMA,
    _FEDERAL_REGISTER_SCOPE,
    _NORMALIZED_FIELDS,
    _PUBLISHER_WITHHOLDING_CODES,
    _RENDITION_ORDER,
    _REQUIRED_NORMALIZED_FIELDS,
    _RESTRICT_REASON_UNREAD,
    _SCHEMA_VERSION,
    _TEST_FIXTURE_ID_PREFIXES,
    _TEST_FIXTURE_REASON_CODE,
    _record_data,
)
from .related_records import (
    _comment_item_from_row,
    _docket_item_from_row,
)
from .sampling import (
    _SAMPLE_DRAWN,
    RegulationsGovSamplePolicy,
    _draw_document_sample,
)


@dataclass(frozen=True, slots=True)
class RegulationsGovCatalogPolicy:
    """Join exact source keys and select capture candidates without producer imports."""

    document_input: SourceInputSelector
    docket_input: SourceInputSelector | None
    federal_register_input: SourceInputSelector | None
    agency_names: Mapping[str, str]
    language: str = "en"
    source_url_template: str = "https://www.regulations.gov/document/{documentId}"
    sample: RegulationsGovSamplePolicy | None = None
    max_selected_items: int | None = None
    comment_input: SourceInputSelector | None = None
    #: Filled on first use by :attr:`policy_digest`; never an input or an
    #: identity, so it stays out of ``__init__``, ``__eq__`` and ``__repr__``.
    _policy_digest: str | None = field(default=None, init=False, repr=False, compare=False)

    policy_id = "urn:docspec:catalog-policy:regulations-gov:1"
    policy_version = "1.2.0"

    def __post_init__(self) -> None:
        expected = (
            (self.document_input, _DOCUMENT_SCOPE, _DOCUMENT_SCHEMA),
            (self.docket_input, _DOCKET_SCOPE, _DOCKET_SCHEMA),
            (self.comment_input, _COMMENT_SCOPE, _COMMENT_SCHEMA),
            (
                self.federal_register_input,
                _FEDERAL_REGISTER_SCOPE,
                _FEDERAL_REGISTER_SCHEMA,
            ),
        )
        for selector, scope_id, schema_name in expected:
            if selector is not None and (
                selector.scope_id != scope_id
                or selector.schema_name != schema_name
                or selector.schema_version != _SCHEMA_VERSION
            ):
                raise ValueError("Regulations.gov catalog input selector differs from its source family")
        names = dict(self.agency_names)
        if any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in names.items()
        ):
            raise ValueError("Regulations.gov agency names must be nonempty text pairs")
        object.__setattr__(self, "agency_names", dict(sorted(names.items(), key=lambda pair: _utf16_key(pair[0]))))
        if not isinstance(self.language, str) or not self.language:
            raise ValueError("Regulations.gov catalog language must be nonempty")
        if not isinstance(self.source_url_template, str) or self.source_url_template.count("{documentId}") != 1:
            raise ValueError("Regulations.gov source URL template must contain one {documentId}")
        if _http_url(self.source_url_template.replace("{documentId}", "probe")) is None:
            raise ValueError("Regulations.gov source URL template must be HTTP(S)")
        if self.max_selected_items is not None and (
            isinstance(self.max_selected_items, bool)
            or not isinstance(self.max_selected_items, int)
            or self.max_selected_items < 1
        ):
            raise ValueError("Regulations.gov selected-item budget must be positive")

    @property
    def universe_inputs(self) -> tuple[SourceInputSelector, ...]:
        return tuple(
            selector
            for selector in (
                self.document_input,
                self.docket_input,
                self.comment_input,
            )
            if selector is not None
        )

    @property
    def configuration(self) -> Mapping[str, Any]:
        return {
            "sourceProfile": "regulations-gov",
            "universeInputs": [selector.to_dict() for selector in self.universe_inputs],
            "docketInput": self.docket_input.to_dict() if self.docket_input is not None else None,
            "commentInput": self.comment_input.to_dict() if self.comment_input is not None else None,
            "federalRegisterInput": (
                self.federal_register_input.to_dict() if self.federal_register_input is not None else None
            ),
            "agencyNames": dict(self.agency_names),
            "language": self.language,
            "sourceUrlTemplate": self.source_url_template,
            "sourceUrlTemplates": {
                "comments": "https://www.regulations.gov/comment/{sourceRecordId}",
                "dockets": "https://www.regulations.gov/docket/{sourceRecordId}",
                "documents": self.source_url_template.replace("{documentId}", "{sourceRecordId}"),
            },
            "sample": self.sample.to_dict() if self.sample is not None else None,
            "maxSelectedItems": self.max_selected_items,
            "normalizationFields": list(_NORMALIZED_FIELDS),
            "requiredNormalizedFields": list(_REQUIRED_NORMALIZED_FIELDS),
            "requiredNormalizedFieldsBySourceKind": {
                "comments": [
                    "agencies",
                    "documentType",
                    "publicationDate",
                    "sourceUrl",
                ],
                "dockets": ["title", "agencies", "lastUpdatedDate", "sourceUrl"],
                "documents": list(_REQUIRED_NORMALIZED_FIELDS),
            },
            "rinNormalization": "federal-register-rin-syntax/1",
            "renditionPreference": list(_RENDITION_ORDER),
            "sourceKindRenditionPreference": {
                "comments": ["regulations-gov-file", "regulations-gov-record"],
                "dockets": ["regulations-gov-record"],
                "documents": list(_RENDITION_ORDER),
            },
            "joins": [
                {
                    "joinId": "comment-docket",
                    "sourceField": "data.attributes.docketId",
                    "lookupScopeId": _DOCKET_SCOPE,
                },
                {
                    "joinId": "comment-document",
                    "sourceField": "data.attributes.commentOnDocumentId",
                    "lookupScopeId": _DOCUMENT_SCOPE,
                },
                {
                    "joinId": "document-docket",
                    "sourceField": "data.attributes.docketId",
                    "lookupScopeId": _DOCKET_SCOPE,
                },
                {
                    "joinId": "document-federal-register",
                    "sourceField": "data.attributes.frDocNum",
                    "lookupScopeId": _FEDERAL_REGISTER_SCOPE,
                },
            ],
            "samplingSourceKinds": ["documents"],
            "sourceIssuedVersionPolicy": {
                "comments": {
                    "primarySourcePath": "data.attributes.modifyDate",
                    "nullFallbackSourcePath": "data.attributes.postedDate",
                },
                "dockets": {"primarySourcePath": "data.attributes.modifyDate"},
                "documents": {
                    "primarySourcePath": "data.attributes.modifyDate",
                    "nullFallbackSourcePath": "data.attributes.postedDate",
                },
            },
            "topicRecovery": {
                "sourceField": "data.attributes.topics",
                "emptyOutcome": "not-recovered",
                "publisherDeclaredEmptyEvidenceDigest": None,
            },
            "publisherWithholding": {
                "sourceField": "data.attributes.restrictReasonType",
                "reasonCodes": dict(_PUBLISHER_WITHHOLDING_CODES),
                "unreadReasonCode": _RESTRICT_REASON_UNREAD,
            },
            "testFixtures": {
                "sourceField": "sourceItemId",
                "idPrefixes": list(_TEST_FIXTURE_ID_PREFIXES),
                "reasonCode": _TEST_FIXTURE_REASON_CODE,
            },
            "selectionFailures": [
                {
                    "decisionId": "publisher-test-fixture",
                    "disposition": "excluded",
                    "reasonCode": _TEST_FIXTURE_REASON_CODE,
                },
                {
                    "decisionId": "source-withdrawal",
                    "disposition": "deleted",
                    "reasonCode": "source.withdrawn-after-publication",
                },
                {
                    "decisionId": "sample-draw",
                    "disposition": "excluded",
                    "reasonCode": "policy.sample-not-drawn",
                },
                {
                    "decisionId": "required-metadata",
                    "disposition": "failed",
                    "reasonCode": "source.normalized-field-missing",
                },
                {
                    "decisionId": "candidate-rendition",
                    "disposition": "unavailable",
                    "reasonCode": "source.no-candidate-rendition",
                },
                *(
                    {
                        "decisionId": "candidate-rendition",
                        "disposition": "unavailable",
                        "reasonCode": code,
                    }
                    for code in _PUBLISHER_WITHHOLDING_CODES.values()
                ),
                {
                    "decisionId": "candidate-rendition",
                    "disposition": "failed",
                    "reasonCode": _RESTRICT_REASON_UNREAD,
                },
                {
                    "decisionId": "selected-item-budget",
                    "disposition": "excluded",
                    "reasonCode": "policy.item-budget-exhausted",
                },
            ],
        }

    def to_member(self) -> dict[str, Any]:
        return {
            "format": "docspec-catalog-policy",
            "formatVersion": "1.0",
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "configuration": dict(self.configuration),
        }

    @property
    def policy_digest(self) -> str:
        """Return this policy's digest, canonicalizing the member at most once.

        The digest is a pure function of the fields, but every interpretation of
        every catalog row stamps it, so the uncached property canonicalized the
        whole policy member once per item. That member is 17,880 bytes here --
        ``configuration`` embeds a 314-entry agency map -- and measured 166.5 us
        per call, so a 49,884-item build spent 8.3 s of its 82.9 s wall clock,
        and a profiled run 26.9 s of 218 s, rebuilding one constant. Worse, the
        cost is O(items x policy size): every agency added to ``agencyNames``
        slowed down every row.

        Filling the cache lazily rather than in ``__post_init__`` keeps
        construction -- and the errors a malformed configuration raises -- exactly
        where they were.
        """

        cached = self._policy_digest
        if cached is not None:
            return cached
        digest = sha256_digest(canonical_json_bytes(self.to_member()))
        object.__setattr__(self, "_policy_digest", digest)
        return digest

    @classmethod
    def from_member(cls, value: object) -> RegulationsGovCatalogPolicy:
        member = closed_mapping(
            value,
            {"format", "formatVersion", "policyId", "policyVersion", "configuration"},
            "Regulations.gov catalog policy",
            error=ValueError,
        )
        configuration = closed_mapping(
            member["configuration"],
            {
                "sourceProfile",
                "universeInputs",
                "docketInput",
                "commentInput",
                "federalRegisterInput",
                "agencyNames",
                "language",
                "sourceUrlTemplate",
                "sourceUrlTemplates",
                "sample",
                "maxSelectedItems",
                "normalizationFields",
                "requiredNormalizedFields",
                "requiredNormalizedFieldsBySourceKind",
                "rinNormalization",
                "renditionPreference",
                "sourceKindRenditionPreference",
                "joins",
                "samplingSourceKinds",
                "sourceIssuedVersionPolicy",
                "topicRecovery",
                "publisherWithholding",
                "testFixtures",
                "selectionFailures",
            },
            "Regulations.gov catalog policy configuration",
            error=ValueError,
        )

        def selector(raw: object) -> SourceInputSelector | None:
            return None if raw is None else SourceInputSelector.from_dict(raw)

        agency_names = configuration["agencyNames"]
        if not isinstance(agency_names, Mapping):
            raise ValueError("Regulations.gov catalog agencyNames must be an object")
        universe_inputs = configuration["universeInputs"]
        if not isinstance(universe_inputs, list) or not universe_inputs:
            raise ValueError("Regulations.gov catalog policy requires universe inputs")
        policy = cls(
            document_input=SourceInputSelector.from_dict(universe_inputs[0]),
            docket_input=selector(configuration["docketInput"]),
            federal_register_input=selector(configuration["federalRegisterInput"]),
            agency_names=dict(agency_names),
            language=configuration["language"],
            source_url_template=configuration["sourceUrlTemplate"],
            sample=(
                RegulationsGovSamplePolicy.from_dict(configuration["sample"])
                if configuration["sample"] is not None
                else None
            ),
            max_selected_items=configuration["maxSelectedItems"],
            comment_input=selector(configuration["commentInput"]),
        )
        if member != policy.to_member():
            raise ValueError("Regulations.gov catalog policy differs from the installed policy version")
        return policy

    def resolve_source_record_collision(
        self,
        selector: SourceInputSelector,
        stored: Mapping[str, Any],
        incoming: Mapping[str, Any],
    ) -> SourceRecordCollisionResolution | None:
        """Pick the owning filing when one document is mirrored under two agencies.

        Regulations.gov publishes a Federal Register document under each agency
        that filed it, so the same ``documentId`` can arrive from two releases.
        DocSpec decision 0004 rules that this is one item with the non-owning
        filing recorded rather than dropped.

        The owner is decided by measurement, not preference. Two tests over all
        1,797,201 document records carrying both a documentId and a docketId --
        the population the rule can be evaluated over -- produce four exceptions
        between them:

        * ``documentId`` starts with ``docketId + "-"`` -- three exceptions.
        * ``docketId`` starts with ``agencyId`` -- one exception.

        A filing that fails either test is the cross-file; the one that passes
        both owns the document. Both blocking records are resolved this way and
        neither is caught by both tests, so the rules are not redundant.

        Prefix *containment* is deliberate, not "docket plus one trailing
        segment": 40,485 of those records carry two-segment sequences such as
        ``DOT-OST-1995-125-0050-0001`` in docket ``DOT-OST-1995-125``, and the
        narrower reading reports every one of them as a violation.

        Returns ``None`` when neither filing can be distinguished, which keeps
        the loader's refusal rather than guessing. This rule answers "which
        mirror owns one document id"; it does not answer "which of two
        differing document ids is canonical", and 0004 measures that it
        resolves none of the 16,652 groups posing that second question.
        """

        candidates = [stored, incoming]
        owners = [row for row in candidates if self._owns_its_filing(row)]
        if len(owners) != 1:
            return None
        owner = owners[0]
        discarded = incoming if owner is stored else stored
        return SourceRecordCollisionResolution(
            owner=owner,
            discarded=discarded,
            reason_code="source.cross-filed-under-another-agency",
            reason=(
                "the same document is mirrored under another agency, whose filing "
                "does not reconstruct its own document id"
            ),
        )

    @staticmethod
    def _owns_its_filing(row: Mapping[str, Any]) -> bool:
        """Both measured tests, which a filing must pass to own its document.

        Reuses ``_record_data`` rather than reaching through the payload here,
        so a shape this policy cannot read is refused in one place. A row whose
        payload is not a readable document simply does not own it, which leaves
        the loader's refusal in charge rather than resolving on a guess.
        """

        try:
            _, attributes = _record_data(row["record"], expected_type="documents")
        except (IntegrityError, KeyError, TypeError):
            return False
        document_id = row["record"].get("sourceRecordId") or ""
        docket_id = attributes.get("docketId") or ""
        agency_id = attributes.get("agencyId") or ""
        if not document_id or not docket_id or not agency_id:
            return False
        return document_id.startswith(f"{docket_id}-") and docket_id.startswith(agency_id)

    def iter_items(
        self,
        inputs: CatalogPolicyInputs,
        workspace: CatalogPolicyWorkspace,
    ) -> Iterator[SourceCatalogItem]:
        resume = getattr(inputs, "resume", FRESH_BUILD)
        if not resume.indexed:
            _index_rows(
                inputs,
                workspace,
                self.federal_register_input,
                _FEDERAL_REGISTER_INDEX,
                key_path=_FEDERAL_REGISTER_KEY_PATH,
            )
            _stage_universe(
                inputs, workspace, index_documents=self.sample is not None or self.comment_input is not None
            )
            if self.sample is not None:
                _draw_document_sample(workspace, sample=self.sample)
        # A resumed run starts past its last committed item with the budget
        # it had reached; forgetting either would publish a different catalog.
        selected_count = resume.selected_count
        for row in inputs.iter_universe_rows():
            record = row.record
            renditions = row.renditions
            source_item_id = str(record["sourceRecordId"])
            budget_available = self.max_selected_items is None or selected_count < self.max_selected_items
            # Conversion reads the pin only when it assembles interpretations.
            # Keeping this lazy preserves source-refusal order and digest caching.
            if record["scopeId"] == _DOCUMENT_SCOPE:
                item = _item_from_row(
                    record,
                    renditions,
                    workspace,
                    discarded_filings=row.discarded_filings,
                    sample_drawn=(
                        workspace.get(_SAMPLE_DRAWN, (source_item_id,)) is not None if self.sample is not None else None
                    ),
                    budget_available=budget_available,
                    agency_names=self.agency_names,
                    interpretation_pin=self._interpretation_pin,
                    language=self.language,
                    max_selected_items=self.max_selected_items,
                    sample=self.sample,
                    source_url_template=self.source_url_template,
                )
            elif record["scopeId"] == _COMMENT_SCOPE:
                item = _comment_item_from_row(
                    record,
                    renditions,
                    workspace,
                    budget_available=budget_available,
                    agency_names=self.agency_names,
                    interpretation_pin=self._interpretation_pin,
                    language=self.language,
                )
            elif record["scopeId"] == _DOCKET_SCOPE:
                item = _docket_item_from_row(
                    record,
                    renditions,
                    budget_available=budget_available,
                    agency_names=self.agency_names,
                    interpretation_pin=self._interpretation_pin,
                    language=self.language,
                )
            else:
                raise IntegrityError("Regulations.gov policy received an undeclared universe scope")
            if item.disposition is CatalogDisposition.SELECTED:
                selected_count += 1
            yield item

    def _input_scope_ids(self) -> list[str]:
        scope_ids = [selector.scope_id for selector in self.universe_inputs]
        if self.federal_register_input is not None:
            scope_ids.append(self.federal_register_input.scope_id)
        return scope_ids

    def _interpretation_pin(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "policyDigest": self.policy_digest,
            "inputScopeIds": self._input_scope_ids(),
        }
