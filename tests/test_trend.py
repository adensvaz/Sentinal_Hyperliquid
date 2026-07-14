"""Trend (CTA) strategy — deterministic unit tests (no network). Verifies dollar-neutral construction,
correct direction (long the strongest uptrends / short the strongest downtrends), and the guards."""
import types

from sentinel.config import TrendCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.trend import TrendStrategy

NAMES = [f"C{i}" for i in range(20)]


def _reg(names=NAMES):
    raw = [{"symbol": n, "type": "E", "marginCoin": "USDC", "multiplier": 0.001, "multiplierCoin": n,
            "pricePrecision": 4, "minOrderVolume": 1, "maxMarketVolume": 0, "minLever": 1,
            "maxLever": 10, "status": 1, "openTakerFee": 0.00045, "closeTakerFee": 0.00045,
            "openMakerFee": 0.00015, "closeMakerFee": 0.00015} for n in names]
    return ContractRegistry(raw)


def _strat(**kw):
    return TrendStrategy(types.SimpleNamespace(trend=TrendCfg(**kw)))


def _up(slope, n=40):   return [100.0 * (1 + slope * i) for i in range(n)]   # rising above its MA
def _down(slope, n=40): return [100.0 * (1 - slope * i) for i in range(n)]   # falling below its MA


def test_dollar_neutral_and_direction():
    closes = {}
    for i in range(10):    closes[f"C{i}"] = _up(0.002 * (i + 1))       # C0..C9 uptrends
    for i in range(10, 20): closes[f"C{i}"] = _down(0.002 * (i - 9))    # C10..C19 downtrends
    prices = {n: closes[n][-1] for n in NAMES}
    book = _strat(top_k=8).build_book(closes, prices, _reg(), 10000.0, 1.0)
    longs = [p for p in book.positions.values() if p.side == "LONG"]
    shorts = [p for p in book.positions.values() if p.side == "SHORT"]
    assert len(longs) == 8 and len(shorts) == 8
    assert all(p.symbol in {f"C{i}" for i in range(10)} for p in longs)        # longs are the uptrends
    assert all(p.symbol in {f"C{i}" for i in range(10, 20)} for p in shorts)   # shorts are the downtrends
    ln = sum(p.target_notional for p in longs)
    sn = sum(p.target_notional for p in shorts)
    assert abs(ln + sn) < 0.03 * abs(ln)                                        # dollar-neutral (net ~ 0)


def test_too_few_candidates_returns_empty():
    sub = NAMES[:10]                                    # 10 < 2*top_k(8)
    book = _strat(top_k=8).build_book({n: _up(0.002) for n in sub},
                                      {n: 100.0 for n in sub}, _reg(), 10000.0, 1.0)
    assert len(book.positions) == 0


def test_all_uptrend_returns_empty():
    # every coin trending up -> no downtrends to short -> empty (needs BOTH sleeves)
    closes = {n: _up(0.002) for n in NAMES}
    prices = {n: closes[n][-1] for n in NAMES}
    book = _strat(top_k=5).build_book(closes, prices, _reg(), 10000.0, 1.0)
    assert len(book.positions) == 0
