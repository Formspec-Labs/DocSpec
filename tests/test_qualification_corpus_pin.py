"""The pinned 10k qualification corpus: identity, verification, and absence.

The corpus itself is a 545 MB local salvage checkpoint, so every test that
needs its bytes skips when it is not on this machine. What does not need the
bytes -- the pin's closed shape, its content-derived identity, and its refusal
of a member that differs -- is checked unconditionally against the committed
pin, because those are the properties that make the mint citable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docspec.domain.identity import canonical_json_file_bytes, stable_urn
from tools.fr_mirrulations_pin import (
    CAMPAIGN_ID,
    PIN_FORMAT,
    PIN_FORMAT_VERSION,
    PIN_IDENTITY_KIND,
    PIN_PATH,
    RESCUE_MAP_FILE,
    RESCUE_MAP_ROLE,
    RUN_NAMES,
    QualificationCorpusError,
    corpus_root,
    load_pin,
)

# D1 in `docs/decisions/0001-document-release-2-0.md`, restated so a pin that
# silently repoints at another catalog fails a test rather than a mint.
FULL_TIER_CATALOG_ID = (
    "urn:docspec:source-catalog:v1:973aaa197206821869294deb09b3cb6281d9bd55ab265214026a48c71fc7d094"
)
FULL_TIER_CATALOG_DIGEST = "sha256:ded6649aab3f04faa6a48f867de0854648ec10c04fcdad8f6527e075d97c45d6"

corpus = pytest.mark.skipif(corpus_root() is None, reason="the pinned qualification corpus is absent")


def _pin() -> dict:
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


def test_the_committed_pin_names_the_catalog_decision_0001_fixed() -> None:
    pin = _pin()
    assert pin["format"] == PIN_FORMAT
    assert pin["formatVersion"] == PIN_FORMAT_VERSION
    assert pin["campaignId"] == CAMPAIGN_ID
    assert pin["tier"] == "full"
    assert pin["sourceCatalog"]["catalogId"] == FULL_TIER_CATALOG_ID
    assert pin["sourceCatalog"]["digest"] == FULL_TIER_CATALOG_DIGEST
    assert tuple(pin["runs"]) == RUN_NAMES


def test_the_pin_carries_both_upstream_draw_digests() -> None:
    draws = _pin()["drawDigests"]
    assert set(draws) == {"federalRegister", "mirrulations"}
    assert draws["federalRegister"]["documents"] == 6408
    assert draws["mirrulations"]["documents"] == 3592
    assert all(value["drawDigest"].startswith("sha256:") for value in draws.values())


def test_the_pin_identity_is_derived_from_its_own_content() -> None:
    pin = _pin()
    content = {name: value for name, value in pin.items() if name != "pinsId"}
    assert pin["pinsId"] == stable_urn(PIN_IDENTITY_KIND, content)


def test_a_pin_whose_identity_was_edited_is_refused(tmp_path: Path) -> None:
    pin = _pin()
    pin["tier"] = "intermediate"
    edited = tmp_path / "pins.json"
    edited.write_bytes(canonical_json_file_bytes(pin))
    with pytest.raises(QualificationCorpusError, match="identity differs"):
        load_pin(edited, root=tmp_path)


@corpus
def test_the_pinned_corpus_loads_and_every_member_digest_holds() -> None:
    pinned = load_pin()
    assert pinned.catalog_id == FULL_TIER_CATALOG_ID
    assert pinned.catalog_digest == FULL_TIER_CATALOG_DIGEST.removeprefix("sha256:")
    snapshot = json.loads(pinned.catalog_bytes.decode("utf-8"))
    assert snapshot["counts"]["items"] == 10000
    assert snapshot["itemsMember"]["recordCount"] == 10000
    assert pinned.items_path.is_file()


@corpus
def test_a_member_that_differs_from_its_pin_is_refused(tmp_path: Path) -> None:
    pin = _pin()
    for member in pin["members"]:
        target = tmp_path / member["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"{}")
    with pytest.raises(QualificationCorpusError, match="differs in size from its pin"):
        load_pin(root=tmp_path)


# ─── Amendment C2: the rescue map is pinned like every other input ─────


def test_the_committed_pin_carries_the_rescue_map_by_path_and_digest() -> None:
    """A mint says which map it read, and a map edited after the fact fails here."""

    rescue = _pin()["rescueMap"]

    assert set(rescue) == {"byteSize", "digest", "mediaType", "path", "role"}
    assert rescue["path"] == RESCUE_MAP_FILE
    assert rescue["role"] == RESCUE_MAP_ROLE
    assert rescue["digest"].startswith("sha256:")
    assert len(rescue["digest"].removeprefix("sha256:")) == 64
    assert rescue["byteSize"] > 0
    # And it is inside the identity, so repointing it renames the pin.
    content = {name: value for name, value in _pin().items() if name != "pinsId"}
    assert "rescueMap" in content


@corpus
def test_a_rescue_map_that_differs_from_its_pin_is_refused(tmp_path: Path) -> None:
    """The same refusal every other pinned byte gets, for the same reason."""

    rescue = tmp_path / "rescue"
    rescue.mkdir()
    (rescue / _pin()["rescueMap"]["path"]).write_bytes(b"not the map")

    with pytest.raises(QualificationCorpusError, match="rescue map differs"):
        load_pin(rescue=rescue)


@corpus
def test_a_pin_declaring_a_rescue_map_that_is_not_here_refuses_rather_than_shrugs(
    tmp_path: Path,
) -> None:
    """A mint that quietly dropped 193 documents would be a silent difference.

    The corpus root is absent-tolerant because a machine without it must skip;
    a rescue map the pin DECLARES and cannot read is different -- continuing
    without it would mint a smaller release under the same procedure, under the
    same pin, and say nothing.
    """

    empty = tmp_path / "rescue"
    empty.mkdir()

    with pytest.raises(QualificationCorpusError, match="rescue map .* is absent"):
        load_pin(rescue=empty)


@corpus
def test_the_pinned_rescue_map_reduces_to_the_index_gap_and_nothing_more() -> None:
    """Amendment C2's two rules, measured against the real checkpoint.

    A map row is adopted only where the checkpoint's own record layer has no
    pointer AND the blob is already in the checkpoint. Under those two rules the
    campaign's complete store map reduces to exactly the index gap -- 194 items,
    387 blobs, no second pointer for anything already captured -- which is what
    makes the broader file a rescue rather than a widening.
    """

    from tools.fr_mirrulations_pin import RESCUE_MAP, preserved_captures, rescued_captures

    pinned = load_pin()
    preserved = preserved_captures(pinned)
    rescued = rescued_captures(pinned, preserved)

    assert len(rescued) == 194
    assert sum(len(by_candidate) for by_candidate in rescued.values()) == 387
    # Nothing the checkpoint already had.
    assert not set(rescued) & set(preserved)
    kinds: dict[str, int] = {}
    for by_candidate in rescued.values():
        for candidate_id, capture in by_candidate.items():
            assert capture.origin == RESCUE_MAP
            assert capture.acquisition_started_at is None
            kinds[candidate_id] = kinds.get(candidate_id, 0) + 1
    assert kinds == {"metadata-json": 194, "rendition-html": 193}
