"""Risk-parity allocator — deterministic unit tests."""
import math
from sentinel.portfolio.allocator import RiskParityAllocator


def test_equal_vol_gives_equal_weight():
    """If all books have the same volatility, weights should be equal."""
    alloc = RiskParityAllocator(lookback=20, min_weight=0.0, max_weight=1.0)
    # same pattern, same vol
    rets = [0.01, -0.01, 0.01, -0.01] * 10
    weights = alloc.compute({"a": rets, "b": rets, "c": rets})
    for w in weights.values():
        assert abs(w - 1/3) < 0.01, f"expected ~0.33, got {w}"


def test_steady_book_gets_more():
    """A book with lower vol should get a HIGHER weight (risk parity)."""
    alloc = RiskParityAllocator(lookback=20, min_weight=0.0, max_weight=1.0)
    steady = [0.001, -0.001] * 20       # very low vol
    volatile = [0.05, -0.05] * 20       # high vol
    weights = alloc.compute({"carry": steady, "trend": volatile})
    assert weights["carry"] > weights["trend"], f"steady should get more: {weights}"
    assert weights["carry"] > 0.9, f"50x less vol -> carry ~98%: {weights}"


def test_weights_sum_to_one():
    alloc = RiskParityAllocator(lookback=15)
    rets = {"a": [0.01*i for i in range(20)],
            "b": [0.02*(-1)**i for i in range(20)],
            "c": [0.005*i for i in range(20)]}
    weights = alloc.compute(rets)
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_min_weight_floor():
    """No book should get less than min_weight."""
    alloc = RiskParityAllocator(lookback=15, min_weight=0.10, max_weight=0.80)
    steady = [0.0001, -0.0001] * 20
    wild = [0.10, -0.10] * 20
    weights = alloc.compute({"steady": steady, "wild": wild, "medium": [0.01, -0.01]*20})
    for w in weights.values():
        assert w >= 0.10 - 1e-6, f"weight below floor: {weights}"


def test_max_weight_cap():
    """No book should get more than max_weight."""
    alloc = RiskParityAllocator(lookback=15, min_weight=0.05, max_weight=0.60)
    steady = [0.0001, -0.0001] * 20   # would naturally get ~95%
    wild = [0.10, -0.10] * 20
    weights = alloc.compute({"steady": steady, "wild": wild})
    for w in weights.values():
        assert w <= 0.60 + 1e-6, f"weight above cap: {weights}"


def test_not_enough_history_falls_back():
    """With < 10 data points, should fall back to equal weights."""
    alloc = RiskParityAllocator(lookback=60, equal_weight_fallback=True)
    weights = alloc.compute({"a": [0.01]*5, "b": [0.02]*5})
    assert abs(weights["a"] - 0.5) < 0.01


def test_single_book():
    alloc = RiskParityAllocator()
    weights = alloc.compute({"only": [0.01]*30})
    assert weights == {"only": 1.0}


def test_empty_returns_empty():
    alloc = RiskParityAllocator()
    assert alloc.compute({}) == {}


def test_format_weights():
    alloc = RiskParityAllocator()
    s = alloc.format_weights({"carry": 0.45, "trend": 0.30, "neutral": 0.25})
    assert "carry=45%" in s
    assert "trend=30%" in s
