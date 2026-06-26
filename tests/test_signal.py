import pytest

from sentinel.signal.base import SymbolData
from sentinel.signal.market_proxy import MarketProxySignal

H4_MS = 4 * 3_600_000


def _series(fn, n=60):
    idx = [1_700_000_000_000 + i * H4_MS for i in range(n)]
    closes = [float(fn(i)) for i in range(n)]
    vols = [1000.0] * n
    return idx, closes, vols


def test_momentum_ordering():
    sig = MarketProxySignal(horizons_hours=[4, 24, 168],
                            weights={"momentum": 1.0, "funding": 0.0, "volume": 0.0})
    up = SymbolData("UP", *_series(lambda i: 100 * (1 + 0.01 * i)))
    down = SymbolData("DOWN", *_series(lambda i: 100 * (1 - 0.005 * i)))
    flat = SymbolData("FLAT", *_series(lambda i: 100))
    data = {sd.symbol: sd for sd in (up, down, flat)}
    for sd in data.values():
        sd.last_price = sd.closes[-1]
    scores = sig.scores(data)
    assert scores["UP"] > scores["FLAT"] > scores["DOWN"]


def test_funding_contrarian_lowers_score():
    # equal flat momentum; the crowded-long (high positive funding) name should score lower
    sig = MarketProxySignal(horizons_hours=[4, 24],
                            weights={"momentum": 0.0, "funding": 1.0, "volume": 0.0},
                            funding_sign=-1)
    a = SymbolData("A", *_series(lambda i: 100)); a.funding = 0.01    # crowded longs
    b = SymbolData("B", *_series(lambda i: 100)); b.funding = -0.01   # crowded shorts
    scores = sig.scores({"A": a, "B": b})
    assert scores["B"] > scores["A"]


def test_funding_velocity_contrarian():
    # equal funding LEVEL and momentum; the name whose funding is ACCELERATING up (crowding building)
    # should score lower under the contrarian sign. Exercises the new funding_vel factor.
    sig = MarketProxySignal(horizons_hours=[4, 24],
                            weights={"momentum": 0.0, "funding": 0.0, "funding_vel": 1.0, "volume": 0.0},
                            funding_sign=-1)
    a = SymbolData("A", *_series(lambda i: 100)); a.funding = 0.0; a.next_funding = 0.01   # rising
    b = SymbolData("B", *_series(lambda i: 100)); b.funding = 0.0; b.next_funding = -0.01  # falling
    scores = sig.scores({"A": a, "B": b})
    assert scores["B"] > scores["A"]


def test_funding_velocity_inert_when_weight_zero():
    # with funding_vel weight 0, next_funding must not affect the score (backtest-path safety)
    sig = MarketProxySignal(horizons_hours=[4, 24],
                            weights={"momentum": 1.0, "funding": 0.0, "funding_vel": 0.0, "volume": 0.0})
    a = SymbolData("A", *_series(lambda i: 100 + i)); a.next_funding = 0.05
    b = SymbolData("B", *_series(lambda i: 100 + i)); b.next_funding = -0.05
    scores = sig.scores({"A": a, "B": b})
    assert scores["A"] == pytest.approx(scores["B"])  # identical momentum -> identical score


def test_drops_symbols_with_too_few_bars():
    sig = MarketProxySignal(horizons_hours=[4], weights={"momentum": 1.0, "funding": 0, "volume": 0})
    good = SymbolData("G", *_series(lambda i: 100 + i, n=10))
    bad = SymbolData("B", idx_ms=[1, 2], closes=[1.0, 2.0], volumes=[1.0, 1.0])
    scores = sig.scores({"G": good, "B": bad})
    assert "G" in scores and "B" not in scores
