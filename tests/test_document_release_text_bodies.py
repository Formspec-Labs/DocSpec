"""Comment and attachment ownership, selection policies, and per-kind accounting."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from docspec.adapters.document_release.coverage import (
    derive_per_kind_counts,
)
from docspec.adapters.document_release.rules import (
    ATTACHMENT_DISPOSITIONS,
    ATTACHMENT_RENDITION_REASON_CODES,
    CATALOG_DISPOSITIONS,
    CATALOG_STATE_REASON_CODES,
    SCHEMA_FILES,
    SOURCE_DISPOSITION_REASON_CODES,
    TEXT_KINDS,
    framed_set_digest,
    stamp_root,
)
from docspec.adapters.document_release.verify import (
    verify_document_release,
)
from docspec.document_release_support import (
    canonical_json_bytes,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
)
from docspec.domain.identity import stable_urn
from tests.support.document_release import (
    ATTACHMENT_TEXT,
    COMMENT_TEXT,
    DOCSPEC_ROOT,
    _rows,
)
from tests.support.document_release import (
    extended as _extended_fixture,
)


# Register the shared fixture in this suite.
extended = _extended_fixture


def _grown(rows: list[dict[str, Any]]) -> int:
    """Where the comment-owned attachment sits among the corpus's own.

    Since amendment B4 the sealed bundle carries two attachments of its own,
    both owned by document bodies. The grown one is the comment-owned row, and
    naming it by that rather than by a position keeps these tests reading the
    thing they are about.
    """

    return next(
        index for index, row in enumerate(rows) if row["ownerKind"] == "comment"
    )


def _restamped(bundle: Path, mutate: Any) -> Any:
    """Apply one mutation to a bundle's state, restamp, and verify."""

    from tools.restamp_document_release_fixtures import _restamp, _state

    state = _state(bundle)
    mutate(state)
    _restamp(bundle, state)
    return verify_document_release(bundle)


def test_a_bundle_carrying_a_comment_and_an_attachment_of_it_verifies_whole(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The sealed extension, exercised end to end rather than merely declared."""

    bundle, _state = extended
    result = verify_document_release(bundle)

    assert [str(issue) for issue in result.issues] == []
    assert result.valid


def test_the_grown_bundle_declares_every_kind_in_its_per_kind_counts(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Amendment A2's fields, and the identity that must hold inside each."""

    bundle, _ = extended
    counts = load_strict_canonical_json(bundle / "release.json")["content"]["counts"]
    per_kind = counts["perKind"]

    assert set(per_kind) == set(TEXT_KINDS)
    # The corpus's own sealed comment plus the grown one, counted from the rows.
    comments = load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl")
    assert per_kind["comment"]["textBodies"] == len(comments) == 2
    assert per_kind["comment"]["representationByteTotal"] == sum(
        row["representation"]["byteSize"] for row in comments
    )
    assert per_kind["comment"]["excludedByteTotal"] == 0
    assert per_kind["comment"]["representationByteTotal"] > len(COMMENT_TEXT)
    # Two of the corpus's own attachments, one of which carries text, plus the
    # grown one: the kinds are counted from the rows, not from a constant.
    attachments = load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
    with_text = [row for row in attachments if row["textBodyId"] is not None]
    assert per_kind["attachment"]["textBodies"] == len(with_text)
    assert per_kind["attachment"]["representationByteTotal"] == sum(
        row["representation"]["byteSize"] for row in with_text
    )
    assert per_kind["attachment"]["representationByteTotal"] > len(ATTACHMENT_TEXT)
    assert per_kind["document-body"]["textBodies"] == 2
    for kind, totals in per_kind.items():
        assert (
            totals["segmentedByteTotal"] + totals["excludedByteTotal"]
            == totals["representationByteTotal"]
        ), kind
    # `coverage` stays aggregate, and is the sum over the kinds.
    coverage = load_strict_canonical_json(bundle / "release.json")["content"]["coverage"]
    for field in ("representationByteTotal", "segmentedByteTotal", "excludedByteTotal"):
        assert coverage[field] == sum(totals[field] for totals in per_kind.values())


def test_a_per_kind_total_that_does_not_balance_is_a_coverage_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The identity is checked per kind, not only where the aggregate balances.

    The mutation moves bytes BETWEEN kinds so the aggregate still balances,
    which is exactly the hole a single aggregate figure would hide.
    """

    bundle, _ = extended
    root = load_strict_canonical_json(bundle / "release.json")
    per_kind = root["content"]["counts"]["perKind"]
    per_kind["comment"]["segmentedByteTotal"] -= 5
    per_kind["attachment"]["segmentedByteTotal"] += 5
    (bundle / "release.json").write_bytes(canonical_json_bytes(stamp_root(root)))

    result = verify_document_release(bundle)
    coverage_paths = [issue.path for issue in result.issues if issue.code == "invalid.coverage"]

    assert "release.json/content/counts/perKind/comment" in coverage_paths
    assert "release.json/content/counts/perKind/attachment" in coverage_paths


def test_the_attachment_id_is_minted_over_the_ordinal_free_preimage(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Amendment A1: the id names the attachment, the ordinal names its renditions."""

    bundle, _ = extended
    rows = load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
    attachment = rows[_grown(rows)]

    assert attachment["attachmentId"] == stable_urn(
        "document-release-attachment",
        {
            "attachmentIdentity": attachment["attachmentIdentity"],
            "ownerKind": attachment["ownerKind"],
            "ownerTextBodyId": attachment["ownerTextBodyId"],
        },
        version=2,
    )
    # Two renditions under one id: an id carrying the ordinal could not do this.
    assert [row["renditionOrdinal"] for row in attachment["renditions"]] == [0, 1]
    assert attachment["textBodyId"] == attachment["attachmentId"]
    schema = json.loads(SCHEMA_FILES["attachments"].read_text(encoding="utf-8"))
    assert "renditionOrdinal" not in schema["properties"]
    assert (
        "renditionOrdinal"
        in schema["$defs"]["rendition"]["properties"]
    )


def test_an_attachment_id_minted_over_another_preimage_is_an_identity_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["attachments"][0]["attachmentIdentity"] = "0900006485a1b2c3-0002.pdf"

    result = _restamped(bundle, mutate)

    assert result.code == "invalid.identity"
    assert result.path == "data/attachments.jsonl/0/attachmentId"


def test_an_attachment_owned_by_nothing_in_this_release_is_a_join_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        attachment = state["attachments"][0]
        attachment["ownerTextBodyId"] = "0900006485deadbeef"
        attachment["attachmentId"] = stable_urn(
            "document-release-attachment",
            {
                "attachmentIdentity": attachment["attachmentIdentity"],
                "ownerKind": attachment["ownerKind"],
                "ownerTextBodyId": attachment["ownerTextBodyId"],
            },
            version=2,
        )
        attachment["textBodyId"] = attachment["attachmentId"]

    result = _restamped(bundle, mutate)
    joins = [issue for issue in result.issues if issue.code == "invalid.join"]

    assert [issue.path for issue in joins] == ["data/attachments.jsonl/0/ownerTextBodyId"]


def test_an_attachment_that_names_its_owner_by_the_wrong_kind_is_a_join_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The owner exists; the row says it is a document body and it is a comment."""

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        attachment = state["attachments"][_grown(state["attachments"])]
        attachment["ownerKind"] = "document-body"
        attachment["attachmentId"] = stable_urn(
            "document-release-attachment",
            {
                "attachmentIdentity": attachment["attachmentIdentity"],
                "ownerKind": attachment["ownerKind"],
                "ownerTextBodyId": attachment["ownerTextBodyId"],
            },
            version=2,
        )
        attachment["textBodyId"] = attachment["attachmentId"]

    result = _restamped(bundle, mutate)
    joins = [issue for issue in result.issues if issue.code == "invalid.join"]

    assert [issue.path for issue in joins] == ["data/attachments.jsonl/2/ownerKind"]


def test_a_failed_rendition_without_a_reason_is_an_accounting_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Loss stays visible: a row may say it could not capture, never say nothing.

    Amendment B4 moved this off `invalid.disposition`. An attachment disposition
    is not a catalog disposition -- Decision 0001 chose the four tokens
    specifically so a reader could not join the two vocabularies -- so the
    diagnostic must not join them either.
    """

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        del state["attachments"][_grown(state["attachments"])]["renditions"][1]["reason"]

    result = _restamped(bundle, mutate)

    assert result.code == "invalid.schema"
    assert any(
        issue.code == "invalid.attachment-accounting"
        and issue.path == "data/attachments.jsonl/2/renditions/1/reason"
        for issue in result.issues
    )


def test_the_four_attachment_disposition_tokens_are_closed_and_not_the_catalogs(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """An attachment disposition is not a catalog disposition, and cannot be read as one."""

    schema = json.loads(SCHEMA_FILES["attachments"].read_text(encoding="utf-8"))
    tokens = schema["$defs"]["rendition"]["properties"]["attachmentDisposition"]["enum"]

    assert tokens == list(ATTACHMENT_DISPOSITIONS)
    assert set(tokens).isdisjoint(CATALOG_DISPOSITIONS)
    bundle, _ = extended
    observed = {
        rendition["attachmentDisposition"]
        for row in load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
        for rendition in row["renditions"]
    }
    assert observed <= set(tokens)


def test_the_closed_reason_code_lists_are_exactly_the_ones_the_decision_records() -> None:
    """Amendment C4: the gate's lists and the decision's lists are one list.

    They are transcribed rather than derived, because the decision is where they
    are decided -- so the thing that can go wrong is exactly a transcription
    drift, and this reads the decision's own fenced blocks back to catch it. A
    code in one and not the other is a defect in whichever is behind, and this
    says which.
    """

    text = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "decisions"
        / "0001-document-release-2-0.md"
    ).read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)```", text, re.DOTALL)

    def codes(marker: str, pattern: str) -> set[str]:
        block = next(body for body in blocks if marker in body)
        return {
            match.group(1)
            for line in block.splitlines()
            # Two spaces, because every list in the decision aligns its
            # descriptions and only a block's prose HEADER is one-spaced.
            if (match := re.match(rf"^({pattern})\s{{2,}}\S", line))
        }

    recorded = codes("catalog.state-deleted", r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*)+")
    recorded |= codes(
        "policy.document-type-out-of-scope", r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*)+"
    )
    assert recorded == SOURCE_DISPOSITION_REASON_CODES
    assert CATALOG_STATE_REASON_CODES < SOURCE_DISPOSITION_REASON_CODES

    # The C4 block, which supersedes B7's two-code one; B7's stays where it is,
    # as every superseded list in this decision does.
    attachment = codes("unmapped-rendition-format", r"[a-z0-9]+(?:-[a-z0-9]+)*")
    assert attachment == ATTACHMENT_RENDITION_REASON_CODES
    # And the corpus spends only codes on the lists, which is what makes the
    # lists a bound rather than a wish.
    for row in _rows("source-dispositions"):
        assert row.get("reasonCode") in SOURCE_DISPOSITION_REASON_CODES | {None}
    for row in _rows("attachments"):
        for rendition in row["renditions"]:
            assert rendition.get("reasonCode") in ATTACHMENT_RENDITION_REASON_CODES | {None}


def test_the_attachment_reason_code_is_a_bounded_kebab_case_string_not_an_enum() -> None:
    """Amendment A3: sealed as a bound now, closed as an enum at the real mint."""

    schema = json.loads(SCHEMA_FILES["attachments"].read_text(encoding="utf-8"))
    reason_code = schema["$defs"]["reasonCode"]

    assert reason_code["type"] == "string"
    assert "enum" not in reason_code
    assert reason_code["maxLength"] == 64
    assert re.fullmatch(reason_code["pattern"], "source-not-found")
    for refused in ("Source-Not-Found", "source_not_found", "source not found", "-source", "x" * 65):
        assert not (
            re.fullmatch(reason_code["pattern"], refused) and len(refused) <= 64
        ), refused


def test_a_comment_row_projects_the_sealed_selection_policy_verbatim(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """DocSpec inherits the refusal, so it must not be able to state another policy."""

    bundle, _ = extended
    comment = load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl")[0]
    schema = json.loads(SCHEMA_FILES["comments"].read_text(encoding="utf-8"))
    policy = schema["$defs"]["commentSelection"]["properties"]

    assert comment["commentId"] == comment["textBodyId"]
    assert policy["groupBy"]["const"] == "/data/id"
    assert policy["orderBy"]["const"] == "/data/attributes/modifyDate DESC NULLS LAST"
    assert policy["tieDisposition"]["const"] == "refuse-repeated-normalized-instant"
    assert comment["commentSelection"]["tieDisposition"] == (
        "refuse-repeated-normalized-instant"
    )


def test_a_repeated_comment_id_is_a_duplicate_identity_not_a_tie_to_resolve(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Handed two observations of one comment id, the build fails rather than picks."""

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["comments"].append(json.loads(json.dumps(state["comments"][0])))

    result = _restamped(bundle, mutate)

    assert result.code == "invalid.duplicate-identity"
    assert result.path == "data/comments.jsonl/2/commentId"


def test_a_comment_filed_against_no_document_in_this_release_is_a_join_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["comments"][0]["documentId"] = "FR-2026-99999"

    result = _restamped(bundle, mutate)
    joins = [issue for issue in result.issues if issue.code == "invalid.join"]

    assert [issue.path for issue in joins] == ["data/comments.jsonl/0/documentId"]


def test_the_set_digests_now_stream_the_rows_rather_than_the_empty_set(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The empty-set digests the corpus carries are a measurement, not a constant."""

    bundle, _ = extended
    content = load_strict_canonical_json(bundle / "release.json")["content"]
    attachments = load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl")
    comments = load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl")

    assert content["attachmentSetDigest"] == framed_set_digest(
        "docspec-attachment-set/3", attachments
    )
    assert content["commentSetDigest"] == framed_set_digest(
        "docspec-comment-set/3", comments
    )
    assert content["attachmentSetDigest"] != DOCSPEC_ROOT["content"]["attachmentSetDigest"]
    assert content["commentSetDigest"] != DOCSPEC_ROOT["content"]["commentSetDigest"]
    # And the text-body set spans all three kinds now.
    assert content["textBodySetDigest"] != DOCSPEC_ROOT["content"]["textBodySetDigest"]


def test_the_per_kind_counts_are_exactly_what_the_members_recompute_to(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The root's breakdown is a recomputation, never an assertion."""

    bundle, _ = extended
    root = load_strict_canonical_json(bundle / "release.json")

    assert root["content"]["counts"]["perKind"] == derive_per_kind_counts(
        load_strict_canonical_jsonl(bundle / "data" / "documents.jsonl"),
        load_strict_canonical_jsonl(bundle / "data" / "attachments.jsonl"),
        load_strict_canonical_jsonl(bundle / "data" / "comments.jsonl"),
        load_strict_canonical_jsonl(bundle / "data" / "search-segments.jsonl"),
        key="textBodyId",
    )


def test_two_selection_policies_in_one_release_is_a_comment_selection_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """Decision 0001's inherited refusal, given the diagnostic it named.

    DocSpec does not select comments; the sealed upstream policy does, and every
    row projects that ONE policy verbatim. Two policy digests in one release is
    the release claiming a selection nobody sealed -- and no schema can see it,
    because a schema reads one row at a time.

    Since amendment C6 the invalid corpus carries this case too
    (`invalid/comment-selection`); this stays because it proves the rule against
    a bundle with a comment the corpus did not mint, and a rule that only holds
    for one producer's rows is not the rule.
    """

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        second = json.loads(json.dumps(state["comments"][0]))
        second["commentId"] = "0900006485000001"
        second["textBodyId"] = second["commentId"]
        second["commentSelection"]["policyDigest"] = "sha256:" + "b" * 64
        # A second comment with no text of its own would break the coverage
        # identity, so it shares the first one's bytes: what is under test is
        # the policy it projects, not the body it carries.
        state["comments"].append(second)
        for row in (*state["nodes"], *state["segments"]):
            if row["textKind"] != "comment":
                continue
            twin = json.loads(json.dumps(row))
            twin["textBodyId"] = second["commentId"]
            for key in ("structuralNodeId", "segmentId"):
                if key in twin:
                    twin[key] = twin[key].replace(row["textBodyId"], second["commentId"])
            if twin.get("structuralParentId"):
                twin["structuralParentId"] = twin["structuralParentId"].replace(
                    row["textBodyId"], second["commentId"]
                )
            (state["nodes"] if "structuralNodeId" in twin else state["segments"]).append(twin)

    result = _restamped(bundle, mutate)
    selection = [issue for issue in result.issues if issue.code == "invalid.comment-selection"]

    assert [issue.path for issue in selection] == [
        "data/comments.jsonl/2/commentSelection/policyDigest"
    ]


def test_a_text_body_no_processing_policy_governs_is_a_retention_floor_defect(
    extended: tuple[Path, dict[str, Any]],
) -> None:
    """The checkable half of "an undeclared floor fails closed" (amendment B4)."""

    bundle, _ = extended

    def mutate(state: dict[str, Any]) -> None:
        state["processingPolicies"] = [
            policy for policy in state["processingPolicies"] if policy["textKind"] != "comment"
        ]

    result = _restamped(bundle, mutate)
    floors = [issue for issue in result.issues if issue.code == "invalid.retention-floor"]

    assert [issue.path for issue in floors] == [
        "data/comments.jsonl/0/capture/mediaType",
        "data/comments.jsonl/1/capture/mediaType",
    ]
    assert "no declared floor" in floors[0].message
