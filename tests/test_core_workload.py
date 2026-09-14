"""Qualify fixture reproducibility, not production capacity."""

from itertools import islice

import pytest

from tests.support.core_reference import value_key
from tests.support.core_workload import CORE_BODY_BYTES, CORE_MEMBER_COUNT, core_members, core_value


def test_core_fixture_has_distinct_occurrences_with_equal_values():
    rows = list(core_members(16))
    assert len({key for key, _, _ in rows}) == 16
    assert len({entity for _, entity, _ in rows}) == 16
    for first, second in zip(rows[::2], rows[1::2], strict=True):
        assert value_key(first[2]) == value_key(second[2])
    assert len({value_key(row[2]["metadata"]) for row in rows}) == 8
    assert all(len(row[2]["body"].encode("ascii")) == CORE_BODY_BYTES for row in rows)


def test_core_fixture_stops_at_the_requested_boundary_and_remains_independent():
    assert len(list(core_members(3))) == 3
    assert list(core_members(0)) == []
    first, second = list(islice(core_members(), 2))
    first[2]["metadata"]["changed"] = True
    assert "changed" not in second[2]["metadata"]
    assert core_value(2_048)["url"] == "https://example.invalid/2048"
    assert core_value(2_049)["url"] == "https://example.invalid/2049"


@pytest.mark.parametrize("count", [-1, CORE_MEMBER_COUNT + 1, True, 1.0])
def test_core_fixture_refuses_invalid_sizes(count):
    with pytest.raises(ValueError, match="member count"):
        list(core_members(count))
