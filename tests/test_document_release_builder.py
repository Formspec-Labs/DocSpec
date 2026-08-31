"""The DocumentRelease 2.0 builder, on a mini-catalog small enough to read.

Every rule Decision 0001 binds on the produce side is checked here against five
synthetic source items -- one below its retention floor, one whose preserved
copy is gone, one whose bytes are multibyte throughout, one offering only JSON,
and one ordinary document -- so the refusals are exercised rather than assumed.
The real 10,000-document mint runs from the same `build_release`; what differs
there is the size of the corpus, not the code.

Nothing here reaches the network or the pinned checkpoint. The preserved
captures are files this test wrote, wrapped in the same `PreservedCapture` the
checkpoint loader produces, so the adopt-and-verify path under test is the one
that runs for real.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.document_release_verify import stamp_root, verify_document_release
from docspec.document_release_support import load_strict_canonical_jsonl, write_canonical_json
from docspec.processing.retention_floors import (
    VISIBLE_TEXT_FRACTION,
    RetentionFloor,
    RetentionFloorRegistry,
)
from tools.build_document_release import (
    BuildInputs,
    build_release,
    sample_universe,
    selected_source_set_digest_from_pin,
)
from tools.fr_mirrulations_pin import PreservedCapture, corpus_root

CATALOG_ID = "urn:docspec:source-catalog:v1:" + "a1" * 32
CATALOG_DIGEST = "b2" * 32
DOCUMENT_BODY = "document-body"

ORDINARY = b"""<RULE>
  <PREAMB>
    <AGENCY TYPE="S">DEPARTMENT OF TRANSPORTATION</AGENCY>
    <SUBJECT>Airworthiness Directives</SUBJECT>
    <SUM>
      <HD SOURCE="HED">SUMMARY:</HD>
      <P>We propose to adopt a new airworthiness directive for certain airplanes, and we invite comment on the proposal from every interested member of the public before the comment period closes.</P>
    </SUM>
    <ACT>
      <HD SOURCE="HED">ACTION:</HD>
      <P>Notice of proposed rulemaking, issued under the authority delegated to the Administrator by the Secretary of Transportation.</P>
    </ACT>
  </PREAMB>
</RULE>
"""

# Visible text far below any declared floor: the markup is the document.
STARVED = b"<RULE>" + b"<PREAMB><AGY><SUB><INNER></INNER></SUB></AGY></PREAMB>" * 12 + b"<P>Short.</P></RULE>"

MULTIBYTE = (
    "<RULE>\n  <SUBJECT>Résumé — Étude sur les régulations</SUBJECT>\n"
    "  <P>Le règlement s'applique aux entreprises françaises, aux sociétés européennes, "
    "et à toute personne morale établie sur le territoire — sans exception aucune.</P>\n"
    "  <P>Une deuxième disposition précise les modalités d'application du présent règlement "
    "pour les années à venir, notamment en matière de contrôle et de sanction.</P>\n</RULE>\n"
).encode("utf-8")

PAGE = (
    b"<html><head><title>Ignored</title></head><body><pre>[Federal Register Volume 85]\n\n"
    b"SECURITIES AND EXCHANGE COMMISSION\n\n"
    b"Notice of a proposed rule change filed by the exchange under section 19 of the Act,\n"
    b"together with the statement of the basis and purpose the exchange supplied.\n\n"
    b"The Commission has received no comment letters on the proposed rule change to date.\n"
    b"</pre></body></html>\n"
)


def _floors() -> RetentionFloorRegistry:
    return RetentionFloorRegistry(
        {
            (DOCUMENT_BODY, "application/xml"): RetentionFloor(
                value="0.35", unit=VISIBLE_TEXT_FRACTION, observed_minimum="0.4777", population="test"
            ),
            (DOCUMENT_BODY, "text/html"): RetentionFloor(
                value="0.59", unit=VISIBLE_TEXT_FRACTION, observed_minimum="0.7976", population="test"
            ),
        }
    )


POLICIES: dict[tuple[str, str], dict[str, str]] = {
    (DOCUMENT_BODY, "application/xml"): {
        "extractorDigest": "sha256:" + "c3" * 32,
        "extractorId": "docspec.xml-visible-text/v1",
    },
    (DOCUMENT_BODY, "text/html"): {
        "extractorDigest": "sha256:" + "d4" * 32,
        "extractorId": "docspec.html-visible-text/v1",
    },
}


# The same carrier as PAGE with different text, so the `attached` item's blob is
# a different file from the `vanished` item's -- content-addressed storage would
# otherwise materialize one for the other and the absence under test would not
# be an absence.
ATTACHED_PAGE = PAGE.replace(b"Notice of a proposed", b"Order approving a proposed")


def _item(
    number: str,
    *,
    media_type: str = "text/xml",
    candidate_id: str = "federal-register-xml",
    digest: str | None = None,
    size: int = 0,
    json_only: bool = False,
) -> dict[str, Any]:
    candidates = []
    if json_only:
        candidates.append(
            {
                "candidateId": "metadata-json",
                "expectedDigest": None,
                "expectedSize": size,
                "locator": f"{number}.json",
                "mediaType": "application/json",
                "metadata": {"publicSourceUrl": f"https://example.gov/{number}.json"},
            }
        )
    else:
        candidates.append(
            {
                "candidateId": candidate_id,
                "expectedDigest": digest,
                "expectedSize": size,
                "locator": f"{number}.xml",
                "mediaType": media_type,
                "metadata": {"publicSourceUrl": f"https://example.gov/{number}"},
            }
        )
    return {
        "candidates": candidates,
        "itemId": f"urn:docspec:qualification:federal-register:{number}",
        "metadata": {
            "qualification": {
                "campaignId": "test",
                "finalDraw": {
                    "agency_slugs": "transportation-department",
                    "document_number": number,
                    "document_type": "Rule",
                    "publication_date": "2005-01-03",
                    "title": f"Test document {number}",
                },
                "source": "federal-register",
                "sourceUrl": f"https://www.federalregister.gov/documents/{number}.xml",
            }
        },
        "state": "active",
        "version": f"sha256:{digest.removeprefix('sha256:') if digest else '0' * 64}",
    }


def _capture(
    directory: Path, item: dict[str, Any], payload: bytes, *, materialize: bool = True
) -> PreservedCapture:
    candidate = item["candidates"][0]
    digest = hashlib.sha256(payload).hexdigest()
    path = directory / f"{digest}.bin"
    if materialize:
        path.write_bytes(payload)
    return PreservedCapture(
        source_item_id=item["itemId"],
        candidate_id=candidate["candidateId"],
        media_type=candidate["mediaType"],
        digest=f"sha256:{digest}",
        byte_size=len(payload),
        path=path,
        acquired_at="2026-08-06T12:00:00Z",
        acquisition_started_at="2026-08-06T07:26:11.200228Z",
        run="full",
    )


def _with_metadata_sibling(
    item: dict[str, Any], directory: Path, body: bytes
) -> PreservedCapture:
    """Give one item the regulations.gov metadata rendition its body's siblings live in.

    The real Mirrulations half of the pinned catalog supplies each document as a
    markup rendition AND an `application/json` metadata rendition, and the
    attachment enumeration Decision 0001 L413 requires is inside the latter, as
    `fileFormats`. The mini universe carries one item of that shape so the
    enumeration is exercised on the packing it actually reads.
    """

    item["candidates"].append(
        {
            "candidateId": "metadata-json",
            "expectedDigest": None,
            "expectedSize": None,
            "locator": "metadata.json",
            "mediaType": "application/json",
            "metadata": {"publicSourceUrl": "https://example.gov/metadata.json"},
        }
    )
    payload = json.dumps(
        {
            "data": {
                "attributes": {
                    "fileFormats": [
                        {
                            "fileUrl": "https://downloads.example.gov/content.pdf",
                            "format": "pdf",
                            "size": 286891,
                        },
                        {
                            "fileUrl": "https://downloads.example.gov/content.htm",
                            "format": "htm",
                            "size": len(body),
                        },
                    ],
                    "postedDate": "2020-01-02T05:00:00Z",
                    "agencyId": "SEC",
                    "documentType": "Notice",
                    "title": "A document published in two formats",
                },
                "id": "SEC-2020-0001-0001",
            }
        },
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    path = directory / f"{digest}.bin"
    path.write_bytes(payload)
    return PreservedCapture(
        source_item_id=item["itemId"],
        candidate_id="metadata-json",
        media_type="application/json",
        digest=f"sha256:{digest}",
        byte_size=len(payload),
        path=path,
        acquired_at="2026-08-06T12:00:00Z",
        acquisition_started_at=None,
        run="full",
    )


@pytest.fixture
def mini(tmp_path: Path) -> dict[str, Any]:
    """A six-item universe: ordinary, starved, gone, multibyte, JSON-only, attached."""

    blobs = tmp_path / "preserved"
    blobs.mkdir()
    plans = [
        ("ordinary", ORDINARY, "text/xml", "federal-register-xml", True, False),
        ("starved", STARVED, "text/xml", "federal-register-xml", True, False),
        ("vanished", PAGE, "text/html", "rendition-html", False, False),
        ("multibyte", MULTIBYTE, "text/xml", "federal-register-xml", True, False),
        ("jsononly", b"{}", "application/json", "metadata-json", True, True),
        ("attached", ATTACHED_PAGE, "text/html", "rendition-html", True, False),
    ]
    items: list[dict[str, Any]] = []
    captures: dict[str, dict[str, Any]] = {}
    for number, payload, media_type, candidate_id, materialize, json_only in plans:
        item = _item(
            number,
            media_type=media_type,
            candidate_id=candidate_id,
            digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            size=len(payload),
            json_only=json_only,
        )
        items.append(item)
        captures[item["itemId"]] = {
            candidate_id: _capture(blobs, item, payload, materialize=materialize)
        }
        if number == "attached":
            captures[item["itemId"]]["metadata-json"] = _with_metadata_sibling(
                item, blobs, payload
            )
    return {"items": items, "captures": captures}


def _inputs(mini: dict[str, Any], **overrides: Any) -> BuildInputs:
    values: dict[str, Any] = {
        "catalog_id": CATALOG_ID,
        "catalog_digest": CATALOG_DIGEST,
        "selected_source_set_digest": selected_source_set_digest_from_pin(mini["items"]),
        "items": mini["items"],
        "captures": mini["captures"],
        "floors": _floors(),
        "extractor_policies": POLICIES,
        "build_run_id": "mini-catalog",
        "published_at": "2026-08-30T00:00:00Z",
    }
    values.update(overrides)
    # Derived from whatever universe this call actually mints over, so a test
    # that widens `items` does not silently declare a pin digest describing a
    # different universe -- the disagreement amendment C3's gate now refuses.
    if "selected_source_set_digest" not in overrides:
        values["selected_source_set_digest"] = selected_source_set_digest_from_pin(
            values["items"]
        )
    return BuildInputs(**values)


def _rows(bundle: Path, name: str) -> list[dict[str, Any]]:
    return load_strict_canonical_jsonl(bundle / "data" / f"{name}.jsonl")


def _dispositions(bundle: Path) -> dict[str, dict[str, Any]]:
    return {row["sourceItemId"].rsplit(":", 1)[1]: row for row in _rows(bundle, "source-dispositions")}


# ─── the gate ──────────────────────────────────────────────────────────


def test_a_minted_bundle_verifies_whole_with_no_diagnostics(tmp_path: Path, mini: dict[str, Any]) -> None:
    bundle = tmp_path / "release"
    root, _report = build_release(bundle, _inputs(mini))
    result = verify_document_release(bundle)
    assert [str(issue) for issue in result.issues] == []
    assert result.code == "valid"
    assert result.release_id == root["releaseId"]
    assert root["releaseId"] == "urn:docspec:document-release:v2:" + root["documentStateDigest"].removeprefix("sha256:")


def test_the_release_pins_the_catalog_every_capture_row_names(tmp_path: Path, mini: dict[str, Any]) -> None:
    bundle = tmp_path / "release"
    root, _ = build_release(bundle, _inputs(mini))
    assert root["content"]["sourceCatalog"] == {
        "catalogDigest": CATALOG_DIGEST,
        "catalogId": CATALOG_ID,
    }
    assert {row["capture"]["catalogReleaseId"] for row in _rows(bundle, "documents")} == {CATALOG_ID}


# ─── capture: adopt and verify, never refetch ──────────────────────────


def test_capture_adopts_the_preserved_bytes_and_proves_them(tmp_path: Path, mini: dict[str, Any]) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    index = {
        (row["family"], row["textBodyId"]): row
        for row in load_strict_canonical_jsonl(bundle / "manifests" / "text-body-index.jsonl")
    }
    for document in _rows(bundle, "documents"):
        slice_row = index[("blob", document["textBodyId"])]
        raw = (bundle / slice_row["member"]).read_bytes()
        carried = raw[slice_row["startByte"] : slice_row["startByte"] + slice_row["byteLength"]]
        assert hashlib.sha256(carried).hexdigest() == document["capture"]["sha256"]
        assert document["capture"]["expectedSha256"] == "sha256:" + document["capture"]["sha256"]


def test_a_preserved_copy_that_is_gone_is_a_capture_failure_not_a_fetch(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    row = _dispositions(bundle)["vanished"]
    assert row["catalogDisposition"] == "unavailable"
    assert row["reasonCode"] == "capture.preserved-copy-unverifiable"
    assert row["documentVersionId"] is None
    assert row["reason"]


def test_an_item_the_checkpoint_never_preserved_is_unavailable(tmp_path: Path, mini: dict[str, Any]) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini, captures={}))
    dispositions = _dispositions(bundle)
    assert {row["catalogDisposition"] for row in dispositions.values()} == {"unavailable", "excluded"}
    assert dispositions["ordinary"]["reasonCode"] == "capture.no-preserved-copy"


def test_preserved_bytes_that_differ_from_their_record_are_refused(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    capture = mini["captures"]["urn:docspec:qualification:federal-register:ordinary"][
        "federal-register-xml"
    ]
    capture.path.write_bytes(ORDINARY + b"<!-- edited after the fact -->")
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    row = _dispositions(bundle)["ordinary"]
    assert row["catalogDisposition"] == "unavailable"
    assert row["reasonCode"] == "capture.preserved-copy-unverifiable"


# ─── extraction: the floor refuses ─────────────────────────────────────


def test_a_parse_below_its_floor_refuses_the_item_with_a_disposition(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    row = _dispositions(bundle)["starved"]
    assert row["catalogDisposition"] == "failed"
    assert row["reasonCode"] == "extraction.below-retention-floor"
    assert "below the declared floor 0.35" in row["reason"]
    assert row["documentVersionId"] is None


def test_an_undeclared_format_refuses_rather_than_inheriting_a_default(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    empty = RetentionFloorRegistry({})
    build_release(bundle, _inputs(mini, floors=empty))
    assert {row["reasonCode"] for row in _dispositions(bundle).values()} == {
        "extraction.retention-floor-undeclared",
        "capture.preserved-copy-unverifiable",
        "selection.no-markup-rendition",
    }


def test_a_json_only_item_is_excluded_for_want_of_a_markup_rendition(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    row = _dispositions(bundle)["jsononly"]
    assert row["catalogDisposition"] == "excluded"
    assert row["reasonCode"] == "selection.no-markup-rendition"


# ─── the universe is fully accounted ───────────────────────────────────


def test_every_member_of_the_universe_has_exactly_one_honest_row(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    root, _ = build_release(bundle, _inputs(mini))
    dispositions = _rows(bundle, "source-dispositions")
    documents = _rows(bundle, "documents")
    counts = root["content"]["counts"]

    assert len(dispositions) == len(mini["items"]) == counts["requestedUniverseCount"]
    assert len({row["sourceItemId"] for row in dispositions}) == len(dispositions)
    assert root["content"]["coverage"]["unaccountedCount"] == 0
    assert (
        counts["selectedCount"]
        + counts["excludedCount"]
        + counts["deletedCount"]
        + counts["unavailableCount"]
        + counts["failedCount"]
        == counts["requestedUniverseCount"]
    )
    versions = {document["documentVersionId"] for document in documents}
    for row in dispositions:
        assert row["processingFailures"] == []
        if row["catalogDisposition"] == "selected":
            assert row["documentVersionId"] in versions
        else:
            assert row["documentVersionId"] is None
            assert row["reason"] and row["reasonCode"]
    assert counts["selectedCount"] == len(documents)


def test_a_selected_row_that_carries_no_document_stops_the_build(
    tmp_path: Path, mini: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.build_document_release as builder

    def _no_documents(*_arguments: Any, **_keywords: Any) -> None:
        raise builder.BuildRefusal("selected", "test.forced", "forced for the test")

    monkeypatch.setattr(builder, "_carry", _no_documents)
    with pytest.raises(SystemExit, match="not one-to-one"):
        build_release(tmp_path / "release", _inputs(mini))


# ─── identity ──────────────────────────────────────────────────────────


def test_identity_is_stable_across_a_rebuild_from_the_same_inputs(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    first, _ = build_release(tmp_path / "one", _inputs(mini))
    second, _ = build_release(
        tmp_path / "two", _inputs(mini, published_at="2027-01-01T00:00:00Z", build_run_id="other")
    )
    assert first["documentStateDigest"] == second["documentStateDigest"]
    assert first["releaseId"] == second["releaseId"]
    assert first["annotations"] != second["annotations"], "the envelope moved and identity did not"


# ─── multibyte bodies ──────────────────────────────────────────────────


def test_a_multibyte_body_keeps_every_offset_on_a_character_boundary(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    document = next(
        row for row in _rows(bundle, "documents") if "multibyte" in row["sourceItemId"]
    )
    index = {
        (row["family"], row["textBodyId"]): row
        for row in load_strict_canonical_jsonl(bundle / "manifests" / "text-body-index.jsonl")
    }
    body_id = document["textBodyId"]
    text_row = index[("text", body_id)]
    raw = (bundle / text_row["member"]).read_bytes()
    body = raw[text_row["startByte"] : text_row["startByte"] + text_row["byteLength"]]
    assert body.decode("utf-8")
    assert "Résumé" in body.decode("utf-8")

    blob_row = index[("blob", body_id)]
    rendition = (bundle / blob_row["member"]).read_bytes()
    rendition = rendition[blob_row["startByte"] : blob_row["startByte"] + blob_row["byteLength"]]
    segments = [row for row in _rows(bundle, "search-segments") if row["textBodyId"] == body_id]
    assert segments
    for segment in segments:
        # Every span decodes on its own: a boundary inside a codepoint would
        # raise here rather than survive into a consumer.
        body[segment["representationStart"] : segment["representationEnd"]].decode("utf-8")
        evidence = segment["evidence"]
        rendition[evidence["start"] : evidence["end"]].decode("utf-8")
        assert evidence["end"] > evidence["start"]
        assert evidence["renditionSha256"] == document["capture"]["sha256"]
    for excluded in document["excludedRanges"]:
        body[excluded["start"] : excluded["end"]].decode("utf-8")


def test_segments_and_exclusions_tile_every_body(tmp_path: Path, mini: dict[str, Any]) -> None:
    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    segments = _rows(bundle, "search-segments")
    for document in _rows(bundle, "documents"):
        spans = [
            (row["representationStart"], row["representationEnd"])
            for row in segments
            if row["textBodyId"] == document["textBodyId"]
        ]
        spans += [(row["start"], row["end"]) for row in document["excludedRanges"]]
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        assert merged == [[0, document["representation"]["byteSize"]]]


# ─── the development sample ────────────────────────────────────────────


def test_a_universe_sample_strides_the_catalog_rather_than_truncating_it() -> None:
    items = [{"n": index} for index in range(100)]
    assert [row["n"] for row in sample_universe(items, 5)] == [0, 20, 40, 60, 80]
    assert sample_universe(items, 500) == items


# ─── the empty members Decision 0001 requires to be present ────────────


def test_the_comment_member_is_present_and_empty(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    """No pinned catalog selects a comment into U, so a zero is written."""

    bundle = tmp_path / "release"
    root, _ = build_release(bundle, _inputs(mini))
    assert (bundle / "data" / "comments.jsonl").read_bytes() == b""
    assert root["content"]["counts"]["perKind"]["comment"]["textBodies"] == 0
    manifest = json.loads((bundle / "manifests" / "global.json").read_text(encoding="utf-8"))
    roles = {member["role"] for member in manifest["members"]}
    assert {"attachments", "comments"} <= roles


def test_a_policy_is_declared_for_every_media_type_the_rows_actually_carry(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    """Amendment C1: the table is keyed the way the rows a consumer joins are.

    The mini catalog's XML half is served as `text/xml` and its floor is
    calibrated under the format key `application/xml`, which is exactly the pair
    the first two real mints got wrong: they declared the key, every row said
    `text/xml`, and the first consumer to join the two refused the bundle.
    """

    bundle = tmp_path / "release"
    root, _ = build_release(bundle, _inputs(mini))
    policies = root["content"]["processingPolicies"]
    carried = {
        (row["textKind"], row["capture"]["mediaType"]) for row in _rows(bundle, "documents")
    }

    assert carried == {(policy["textKind"], policy["mediaType"]) for policy in policies}
    assert ("document-body", "text/xml") in carried
    # The floor beside it is still the one calibrated under the collapsed key,
    # which is the whole point of keeping the collapse on the lookup side alone.
    xml = next(policy for policy in policies if policy["mediaType"] == "text/xml")
    assert xml["retentionFloor"] == _floors().floor_for("document-body", "text/xml").to_dict()


def test_a_policy_declared_under_the_format_key_no_longer_governs_its_rows(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    """The defect itself, reconstructed: the gate used to collapse and pass it.

    `application/xml` and `text/xml` are one retention population, so the check
    that collapsed both sides before comparing could not see the difference
    between a policy table a consumer can join and one it cannot. Matching
    literally is what makes this a refusal.
    """

    bundle = tmp_path / "release"
    build_release(bundle, _inputs(mini))
    root = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    for policy in root["content"]["processingPolicies"]:
        if policy["mediaType"] == "text/xml":
            policy["mediaType"] = "application/xml"
    write_canonical_json(bundle / "release.json", stamp_root(root))

    result = verify_document_release(bundle)
    floors = [issue for issue in result.issues if issue.code == "invalid.retention-floor"]

    assert floors
    assert all(issue.path.endswith("/capture/mediaType") for issue in floors)
    assert "'text/xml'" in floors[0].message


def test_every_enumerated_attachment_gets_a_row_and_an_honest_disposition(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    """Decision 0001 L413, landed by amendment B4.

    The mini universe's items enumerate a `pdf` and an `htm` in the source
    record their preserved metadata rendition carries, exactly as the real
    corpus's Mirrulations half does. One row groups both -- the flat packing is
    a list of RENDITIONS, not of attachments -- and the `htm` that IS the owning
    body's rendition is `text-excluded` rather than extracted a second time.
    """

    bundle = tmp_path / "release"
    root, _ = build_release(bundle, _inputs(mini))
    attachments = _rows(bundle, "attachments")
    documents = {row["textBodyId"] for row in _rows(bundle, "documents")}

    assert attachments
    for row in attachments:
        assert row["ownerKind"] == "document-body"
        assert row["ownerTextBodyId"] in documents
        assert row["attachmentTitle"] is None
        # Enumerated, not extracted: no rendition of a document's own content
        # file becomes a second text body.
        assert row["textBodyId"] is None
        assert row["representation"] is None
        assert [rendition["renditionOrdinal"] for rendition in row["renditions"]] == list(
            range(len(row["renditions"]))
        )
        for rendition in row["renditions"]:
            assert rendition["attachmentDisposition"] in {"text-excluded", "source-unavailable"}
            assert rendition["reasonCode"] and rendition["reason"]
    accounting = root["content"]["counts"]["attachmentAccounting"]

    assert accounting["attachmentRows"] == len(attachments)
    assert accounting["textCaptured"] == 0
    assert accounting["extractionFailed"] == 0
    assert accounting["textExcluded"] + accounting["sourceUnavailable"] == accounting[
        "renditionRows"
    ]
    assert root["content"]["counts"]["perKind"]["attachment"]["textBodies"] == 0
    assert verify_document_release(bundle).valid


# ─── the real corpus, bounded ──────────────────────────────────────────
#
# The pinned checkpoint is a 545 MB local salvage, so this skips on any machine
# that does not have it. What it proves is what a synthetic catalog cannot: that
# the real preserved bytes, the real committed floors, and the real pinned
# catalog produce a bundle the gate accepts. It mints over a deterministic
# 200-item stride rather than the whole universe -- the full 10,000-document
# mint is a CLI run whose receipt is committed under `docs/history/`, not a unit
# test.

corpus = pytest.mark.skipif(
    corpus_root() is None, reason="the pinned qualification corpus is absent"
)


@corpus
def test_a_sample_of_the_real_corpus_mints_and_verifies(tmp_path: Path) -> None:
    from tools.build_document_release import mint

    receipt = mint(tmp_path / "release", universe_sample=200, published_at="2026-08-30T00:00:00Z")

    assert receipt["verification"] == {
        "code": "valid",
        "diagnosticCount": 0,
        "diagnostics": [],
        "generation": "docspec",
    }
    counts = receipt["counts"]
    assert counts["requestedUniverseCount"] == 200
    assert sum(receipt["dispositions"].values()) == 200
    assert counts["selectedCount"] == counts["documentVersionCount"] > 0
    # Both formats the pinned catalog carries reach the release, each under its
    # own declared floor -- and each keyed by the media type its CAPTURE ROWS
    # carry (amendment C1), not by the retention format key those collapse onto.
    # `text/xml` here rather than `application/xml` is the whole defect: the
    # first two mints declared the key and every Federal Register row said
    # `text/xml`, so the first consumer to join the table found no policy.
    assert set(receipt["retention"]) == {"text/xml", "text/html"}
    assert {policy["mediaType"] for policy in receipt["processingPolicies"]} == {
        "text/xml",
        "text/html",
    }
    captured = {
        row["capture"]["mediaType"]
        for row in load_strict_canonical_jsonl(
            tmp_path / "release" / "data" / "documents.jsonl"
        )
    }
    assert captured == {policy["mediaType"] for policy in receipt["processingPolicies"]}
    assert receipt["maxObservedSegmentBytes"] <= 65_536
    assert receipt["corpusPin"]["catalogId"].startswith("urn:docspec:source-catalog:v1:")
    assert receipt["releaseId"] == "urn:docspec:document-release:v2:" + receipt[
        "documentStateDigest"
    ].removeprefix("sha256:")


@corpus
def test_the_real_mint_is_reproducible_from_the_same_pinned_bytes(tmp_path: Path) -> None:
    from tools.build_document_release import mint

    first = mint(tmp_path / "one", universe_sample=40, published_at="2026-08-30T00:00:00Z")
    second = mint(tmp_path / "two", universe_sample=40, published_at="2027-02-02T02:02:02Z")
    assert first["documentStateDigest"] == second["documentStateDigest"]
    assert first["setDigests"] == second["setDigests"]


# ─── Amendment C2: the rescue map, hermetically ────────────────────────
#
# Nothing below touches the pinned checkpoint. The tree these tests write IS the
# shape `rescued_captures` reads -- a content-addressed blob store under
# `runs/<run>/blobs/objects/sha256/<xx>/<digest>`, and a gzipped store map whose
# rows carry the record layer's own fields -- so the path under test is the one
# that runs for real, over bytes this test can state exactly.


RESCUE_NUMBER = "rescued-01"
# The catalog's own item id, which is what a store-map row keys on.
RESCUE_ITEM = f"urn:docspec:qualification:federal-register:{RESCUE_NUMBER}"


def _checkpoint(tmp_path: Path, blobs: dict[str, bytes], *, run: str = "full") -> Path:
    """A checkpoint-shaped blob store holding exactly the bytes given, by digest."""

    root = tmp_path / "checkpoint"
    for digest, payload in blobs.items():
        path = root / "runs" / run / "blobs" / "objects" / "sha256" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def _rescue_map(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "store-map.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def _map_row(
    item_id: str, candidate_id: str, media_type: str, digest: str, size: int, *, run: str = "full"
) -> dict[str, Any]:
    return {
        "capturedFiles": [
            {
                "acquiredAt": "2026-08-06T12:00:00Z",
                "blob": {
                    "byteSize": size,
                    "digest": f"sha256:{digest}",
                    "locator": f"objects/sha256/{digest[:2]}/{digest}",
                    "mediaType": media_type,
                },
                "candidateId": candidate_id,
                "disposition": "captured",
                "mediaType": media_type,
                "sourceItemId": item_id,
            }
        ],
        "change": "added",
        "origin": "member",
        "run": run,
        "sourceItemId": item_id,
    }


def _pinned(root: Path, rescue_map: Path) -> Any:
    from tools.fr_mirrulations_pin import PinnedCorpus

    return PinnedCorpus(
        pins_id="urn:docspec:qualification-corpus-pin:v1:" + "0" * 64,
        root=root,
        campaign_id="test",
        tier="full",
        catalog_id=CATALOG_ID,
        catalog_digest=CATALOG_DIGEST,
        catalog_locator="catalog.json",
        catalog_bytes=b"{}",
        items_path=root / "items.jsonl",
        draw_digests={},
        rescue_map_path=rescue_map,
        rescue_map_digest=hashlib.sha256(rescue_map.read_bytes()).hexdigest(),
    )


def _rescued_item(payload: bytes) -> dict[str, Any]:
    """One catalog item whose only body rendition is the rescued one."""

    return _item(
        RESCUE_NUMBER,
        media_type="text/html",
        candidate_id="rendition-html",
        # No expected digest, exactly as the Mirrulations half of the real
        # catalog carries: the rescue is verified against the acquisition
        # record's digest, which is the check that always applies.
        digest=None,
        size=len(payload),
    )


def test_the_rescue_map_names_a_preserved_copy_the_record_layer_missed(tmp_path: Path) -> None:
    """Amendment C2's whole claim: the bytes were there and the index was not."""

    from tools.fr_mirrulations_pin import RESCUE_MAP, rescued_captures

    payload = PAGE
    digest = hashlib.sha256(payload).hexdigest()
    root = _checkpoint(tmp_path, {digest: payload})
    pinned = _pinned(
        root,
        _rescue_map(
            tmp_path,
            [_map_row(RESCUE_ITEM, "rendition-html", "text/html", digest, len(payload))],
        ),
    )

    found = rescued_captures(pinned, {})

    assert set(found) == {RESCUE_ITEM}
    capture = found[RESCUE_ITEM]["rendition-html"]
    assert capture.origin == RESCUE_MAP
    assert capture.read() == payload
    # The map records no acquisition START, so the row says null rather than
    # borrowing one from anywhere.
    assert capture.acquisition_started_at is None
    assert capture.acquired_at == "2026-08-06T12:00:00Z"


def test_a_rescued_item_is_selected_and_carried_like_any_preserved_copy(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    """The rescue is a pointer, so what it points at is carried by the ordinary path."""

    from tools.fr_mirrulations_pin import rescued_captures

    payload = PAGE
    digest = hashlib.sha256(payload).hexdigest()
    root = _checkpoint(tmp_path, {digest: payload})
    pinned = _pinned(
        root,
        _rescue_map(
            tmp_path,
            [_map_row(RESCUE_ITEM, "rendition-html", "text/html", digest, len(payload))],
        ),
    )
    item = _rescued_item(payload)
    item_id = item["itemId"]
    captures = dict(mini["captures"])
    for owner, by_candidate in rescued_captures(pinned, captures).items():
        captures.setdefault(owner, {}).update(by_candidate)

    bundle = tmp_path / "release"
    build_release(
        bundle,
        _inputs(mini, items=[*mini["items"], item], captures=captures),
    )

    row = next(
        entry for entry in _rows(bundle, "source-dispositions") if entry["sourceItemId"] == item_id
    )
    assert row["catalogDisposition"] == "selected"
    assert row["documentVersionId"] is not None
    document = next(
        entry for entry in _rows(bundle, "documents") if entry["sourceItemId"] == item_id
    )
    assert document["capture"]["sha256"] == digest
    assert document["capture"]["byteSize"] == len(payload)
    assert document["capture"]["acquisitionStartedAt"] is None
    assert verify_document_release(bundle).valid


def test_a_rescued_blob_that_is_not_what_the_map_records_is_a_capture_failure(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    """A mismatch is a capture failure, never a reason to fetch.

    The map names a digest, the store holds other bytes under that name, and the
    ordinary adopt-and-verify check refuses them. The item is `unavailable` with
    `capture.preserved-copy-unverifiable`; nothing reaches for the real bytes.
    """

    from tools.fr_mirrulations_pin import rescued_captures

    payload = PAGE
    claimed = hashlib.sha256(payload).hexdigest()
    # Under the name of `payload`'s digest, different bytes.
    root = _checkpoint(tmp_path, {claimed: ATTACHED_PAGE})
    pinned = _pinned(
        root,
        _rescue_map(
            tmp_path,
            [_map_row(RESCUE_ITEM, "rendition-html", "text/html", claimed, len(payload))],
        ),
    )
    item = _rescued_item(payload)
    captures = dict(mini["captures"])
    for owner, by_candidate in rescued_captures(pinned, captures).items():
        captures.setdefault(owner, {}).update(by_candidate)

    bundle = tmp_path / "release"
    build_release(
        bundle,
        _inputs(mini, items=[*mini["items"], item], captures=captures),
    )

    row = next(
        entry
        for entry in _rows(bundle, "source-dispositions")
        if entry["sourceItemId"] == item["itemId"]
    )
    assert row["catalogDisposition"] == "unavailable"
    assert row["reasonCode"] == "capture.preserved-copy-unverifiable"
    assert row["documentVersionId"] is None
    assert verify_document_release(bundle).valid


def test_the_rescue_map_never_supplies_bytes_the_checkpoint_does_not_hold(
    tmp_path: Path,
) -> None:
    """Rule two: a rescue that had to reach for bytes would be a fetch."""

    from tools.fr_mirrulations_pin import rescued_captures

    absent = hashlib.sha256(b"never preserved").hexdigest()
    pinned = _pinned(
        _checkpoint(tmp_path, {}),
        _rescue_map(tmp_path, [_map_row(RESCUE_ITEM, "rendition-html", "text/html", absent, 15)]),
    )

    assert rescued_captures(pinned, {}) == {}


def test_the_rescue_map_never_overrides_a_pointer_the_checkpoint_already_has(
    tmp_path: Path,
) -> None:
    """Rule one: it fills an index gap, it does not compete with the index."""

    from tools.fr_mirrulations_pin import PreservedCapture, rescued_captures

    payload = PAGE
    digest = hashlib.sha256(payload).hexdigest()
    root = _checkpoint(tmp_path, {digest: payload})
    pinned = _pinned(
        root,
        _rescue_map(
            tmp_path,
            [_map_row(RESCUE_ITEM, "rendition-html", "text/html", digest, len(payload))],
        ),
    )
    recorded = PreservedCapture(
        source_item_id=RESCUE_ITEM,
        candidate_id="rendition-html",
        media_type="text/html",
        digest=f"sha256:{digest}",
        byte_size=len(payload),
        path=root / "elsewhere",
        acquired_at="2026-08-06T12:00:00Z",
        acquisition_started_at="2026-08-21T17:02:39.728217Z",
        run="full",
    )

    assert rescued_captures(pinned, {RESCUE_ITEM: {"rendition-html": recorded}}) == {}
