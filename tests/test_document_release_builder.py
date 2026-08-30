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

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.document_release_verify import verify_document_release
from docspec.document_release_support import load_strict_canonical_jsonl
from docspec.processing.retention_floors import (
    VISIBLE_TEXT_FRACTION,
    RetentionFloor,
    RetentionFloorRegistry,
)
from tools.build_document_release import (
    BuildInputs,
    build_release,
    sample_universe,
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


@pytest.fixture
def mini(tmp_path: Path) -> dict[str, Any]:
    """A five-item universe: one ordinary, one starved, one gone, one multibyte, one JSON."""

    blobs = tmp_path / "preserved"
    blobs.mkdir()
    plans = [
        ("ordinary", ORDINARY, "text/xml", "federal-register-xml", True, False),
        ("starved", STARVED, "text/xml", "federal-register-xml", True, False),
        ("vanished", PAGE, "text/html", "rendition-html", False, False),
        ("multibyte", MULTIBYTE, "text/xml", "federal-register-xml", True, False),
        ("jsononly", b"{}", "application/json", "metadata-json", True, True),
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
    return {"items": items, "captures": captures}


def _inputs(mini: dict[str, Any], **overrides: Any) -> BuildInputs:
    values: dict[str, Any] = {
        "catalog_id": CATALOG_ID,
        "catalog_digest": CATALOG_DIGEST,
        "items": mini["items"],
        "captures": mini["captures"],
        "floors": _floors(),
        "extractor_policies": POLICIES,
        "build_run_id": "mini-catalog",
        "published_at": "2026-08-30T00:00:00Z",
    }
    values.update(overrides)
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


def test_the_attachment_and_comment_members_are_present_and_empty(
    tmp_path: Path, mini: dict[str, Any]
) -> None:
    bundle = tmp_path / "release"
    root, _ = build_release(bundle, _inputs(mini))
    assert (bundle / "data" / "attachments.jsonl").read_bytes() == b""
    assert (bundle / "data" / "comments.jsonl").read_bytes() == b""
    per_kind = root["content"]["counts"]["perKind"]
    assert per_kind["attachment"]["textBodies"] == 0
    assert per_kind["comment"]["textBodies"] == 0
    manifest = json.loads((bundle / "manifests" / "global.json").read_text(encoding="utf-8"))
    roles = {member["role"] for member in manifest["members"]}
    assert {"attachments", "comments"} <= roles


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
    # own declared floor.
    assert set(receipt["retention"]) == {"application/xml", "text/html"}
    assert {policy["mediaType"] for policy in receipt["processingPolicies"]} == {
        "application/xml",
        "text/html",
    }
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
