"""Retention floors: declared, measured, and fail-closed.

Decision 0001's *Acceptance gates* turn extraction from something that counts
into something that refuses. These check the three refusals it names -- below
the floor, no declared floor, and an unmeasurable source -- the two invariants
a floor must satisfy before it may govern anything, and the committed
calibration receipt the builder reads its floors from.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from docspec.processing.retention_floors import (
    BELOW_FLOOR,
    FLOOR_UNDECLARED,
    UNMEASURABLE,
    NORMALIZED_VISIBLE_TEXT_FRACTION,
    VISIBLE_TEXT_FRACTION,
    RetentionFloor,
    RetentionFloorError,
    RetentionFloorRegistry,
    decimal_fraction,
    format_key,
    greater,
)
from tools.calibrate_retention_floors import load_floors, load_policies

DOCUMENT_BODY = "document-body"


def floor(value: str = "0.5", observed: str = "0.7718") -> RetentionFloor:
    return RetentionFloor(
        value=value, unit=VISIBLE_TEXT_FRACTION, observed_minimum=observed, population="test"
    )


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("text/xml", "application/xml"),
        ("application/xml", "application/xml"),
        ("application/rss+xml", "application/xml"),
        ("TEXT/XML; charset=utf-8", "application/xml"),
        ("text/html", "text/html"),
        ("application/json", "application/json"),
    ],
)
def test_media_types_collapse_onto_one_format_key(media_type: str, expected: str) -> None:
    assert format_key(media_type) == expected


def test_the_fixture_corpus_retention_is_reproduced_from_its_own_byte_counts() -> None:
    # The sealed conformance bundle records `observedMinimum` "0.7718" over its
    # own two documents. Restated here so a change to the truncation rule fails
    # a test rather than silently renaming twenty sealed bundles.
    assert decimal_fraction(203, 263) == "0.7718"
    assert decimal_fraction(380, 472) == "0.805"


def test_a_measurement_is_truncated_never_rounded_up() -> None:
    assert decimal_fraction(1999, 10000) == "0.1999"
    assert decimal_fraction(19999, 100000) == "0.1999"


@pytest.mark.parametrize(("numerator", "denominator"), [(0, 10), (10, 10), (11, 10), (1, 100000)])
def test_a_ratio_that_is_not_a_fraction_below_one_is_refused(numerator: int, denominator: int) -> None:
    with pytest.raises(ValueError):
        decimal_fraction(numerator, denominator)


def test_decimal_strings_compare_without_a_float() -> None:
    assert greater("0.9", "0.85")
    assert greater("0.1001", "0.1")
    assert not greater("0.1", "0.1")


@pytest.mark.parametrize("value", ["0", "1", "1.0", "0.0", "0.50", "5e-1", "0.5.1"])
def test_a_floor_outside_the_open_unit_interval_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        floor(value=value)


def test_a_floor_with_no_margin_under_the_lowest_document_is_refused() -> None:
    with pytest.raises(ValueError, match="no margin"):
        floor(value="0.8", observed="0.7718")
    with pytest.raises(ValueError, match="no margin"):
        floor(value="0.7718", observed="0.7718")


def test_a_floor_round_trips_through_the_wire_shape() -> None:
    assert RetentionFloor.from_dict(floor().to_dict()) == floor()


def test_an_undeclared_parser_and_format_is_refused_never_defaulted() -> None:
    registry = RetentionFloorRegistry({(DOCUMENT_BODY, "application/xml"): floor()})
    with pytest.raises(RetentionFloorError) as raised:
        registry.admit(DOCUMENT_BODY, "application/pdf", retained=90, source=100)
    assert raised.value.reason_code == FLOOR_UNDECLARED


def test_a_floor_declared_for_one_kind_does_not_govern_another() -> None:
    registry = RetentionFloorRegistry({(DOCUMENT_BODY, "text/html"): floor()})
    with pytest.raises(RetentionFloorError) as raised:
        registry.admit("comment", "text/html", retained=90, source=100)
    assert raised.value.reason_code == FLOOR_UNDECLARED


def test_a_parse_below_its_floor_is_refused_with_the_measurement_in_the_reason() -> None:
    registry = RetentionFloorRegistry({(DOCUMENT_BODY, "text/html"): floor(value="0.5")})
    with pytest.raises(RetentionFloorError) as raised:
        registry.admit(DOCUMENT_BODY, "text/html", retained=49, source=100)
    assert raised.value.reason_code == BELOW_FLOOR
    assert "0.49" in raised.value.reason


def test_a_parse_at_its_floor_is_admitted() -> None:
    registry = RetentionFloorRegistry({(DOCUMENT_BODY, "text/html"): floor(value="0.5")})
    assert registry.admit(DOCUMENT_BODY, "text/html", retained=50, source=100) == "0.5"


@pytest.mark.parametrize(("retained", "source"), [(0, 100), (10, 0), (100, 100), (120, 100)])
def test_an_unmeasurable_source_is_refused_rather_than_admitted(retained: int, source: int) -> None:
    registry = RetentionFloorRegistry({(DOCUMENT_BODY, "text/html"): floor(value="0.5")})
    with pytest.raises(RetentionFloorError) as raised:
        registry.admit(DOCUMENT_BODY, "text/html", retained=retained, source=source)
    assert raised.value.reason_code == UNMEASURABLE


def test_the_committed_calibration_declares_a_floor_for_every_format_this_mint_reads() -> None:
    floors = load_floors()
    assert set(floors) == {(DOCUMENT_BODY, "application/xml"), (DOCUMENT_BODY, "text/html")}
    for key, declared in floors.items():
        # Amendment B5: the markup unit is the whitespace-normalized ratio now.
        # `visible-text-fraction` survives only in the sealed predecessor corpus
        # and in the superseded first mint.
        assert declared.unit == NORMALIZED_VISIBLE_TEXT_FRACTION
        assert declared.unit != VISIBLE_TEXT_FRACTION
        assert greater(declared.observed_minimum, declared.value), key
        assert declared.population


def test_every_committed_floor_is_measured_whole_and_off_the_corpus_it_gates() -> None:
    """Amendment B5's three honesty rules, read off the committed receipt.

    `observedMinimum` over the FULL population, the population disjoint from the
    gated corpus both by digest and by document id, and both ratios recorded per
    document -- the raw one because normalization hides content whose meaning is
    carried by its whitespace.
    """

    from tools.calibrate_retention_floors import load_receipt

    receipt = load_receipt()

    assert receipt["metric"]["metricId"] == NORMALIZED_VISIBLE_TEXT_FRACTION
    for measurement in receipt["measurements"]:
        population = measurement["population"]
        assert population["coverage"] == "full-population"
        assert population["measuredCount"] == population["documentCount"]
        assert len(measurement["documents"]) == population["documentCount"]
        assert measurement["observedMinimum"] == measurement["distribution"]["minimum"]
        assert measurement["observedMinimum"] == min(
            row["retention"].ljust(10, "0") for row in measurement["documents"]
        ).rstrip("0")
        assert population["disjointness"]["sharedRenditionDigests"] == 0
        assert population["disjointness"]["sharedSourceDocuments"] == 0
        for row in measurement["documents"]:
            assert row["retention"] and row["rawRetention"]
        # The floors are calibrated where they are NOT gating: the pinned corpus
        # contributes no document to either population.
        assert "fr-mirrulations" not in population["populationId"]


def test_every_committed_floor_names_the_extractor_that_measured_it() -> None:
    policies = load_policies()
    assert set(policies) == set(load_floors())
    for policy in policies.values():
        assert policy["extractorId"].startswith("docspec.")
        assert len(policy["extractorDigest"].removeprefix("sha256:")) == 64


# ─── Amendment C5: the receipt is recomputed, never merely self-consistent ──


def _committed_receipt() -> dict[str, Any]:
    from tools.calibrate_retention_floors import load_receipt

    return json.loads(json.dumps(load_receipt()))


@pytest.mark.parametrize(
    ("name", "doctor"),
    [
        (
            "a distribution that agrees with itself and with no row",
            lambda m: m["distribution"].update(minimum="0.4", median="0.4"),
        ),
        (
            "an observed minimum moved in lockstep with the distribution",
            lambda m: (
                m.__setitem__("observedMinimum", "0.4"),
                m["distribution"].__setitem__("minimum", "0.4"),
                m["retentionFloor"].__setitem__("observedMinimum", "0.4"),
            ),
        ),
        (
            "a document row whose declared ratio is not its own byte counts",
            lambda m: m["documents"][0].__setitem__("retention", "0.9999"),
        ),
        (
            "a lowest document that is not the lowest row",
            lambda m: m.__setitem__("lowestDocument", m["documents"][0]),
        ),
        (
            "a floor loosened under an untouched observed minimum",
            lambda m: m["retentionFloor"].__setitem__("value", "0.01"),
        ),
    ],
)
def test_a_receipt_that_only_agrees_with_itself_is_refused(name: str, doctor: Any) -> None:
    """The lie amendment B5 found was self-consistent, so consistency is not the test.

    Each mutation below leaves the receipt internally coherent -- and schema
    valid -- and disagrees with the 993 document rows it carries. Every one is a
    receipt the old `validate_receipt` admitted.
    """

    from tools.calibrate_retention_floors import validate_receipt

    receipt = _committed_receipt()
    doctor(receipt["measurements"][0])

    with pytest.raises(ValueError):
        validate_receipt(receipt)


def test_the_committed_receipt_survives_the_recomputation_it_now_faces() -> None:
    """And the one in the repository is not doctored, which is the other half."""

    from tools.calibrate_retention_floors import validate_receipt

    validate_receipt(_committed_receipt())
