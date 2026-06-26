import pytest

from sentinel.util.mathx import (parse_duration_to_hours, value_at_or_before,
                                 zscore)


def test_zscore_flat_is_zeros():
    assert zscore([5, 5, 5]) == [0.0, 0.0, 0.0]


def test_zscore_basic():
    assert zscore([0, 10]) == [-1.0, 1.0]


def test_value_at_or_before():
    idx = [10, 20, 30]
    vals = [1.0, 2.0, 3.0]
    assert value_at_or_before(idx, vals, 25) == 2.0
    assert value_at_or_before(idx, vals, 30) == 3.0
    assert value_at_or_before(idx, vals, 100) == 3.0
    assert value_at_or_before(idx, vals, 5) is None


@pytest.mark.parametrize("s,hours", [
    ("4h", 4), ("24h", 24), ("7d", 168), ("30min", 0.5),
    ("1day", 24), ("1week", 168), ("1month", 720),
])
def test_parse_duration(s, hours):
    assert parse_duration_to_hours(s) == hours
