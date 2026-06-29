"""Tests for the funding-carry + momentum combo strategy (market-neutral diversifier book)."""
import os
import tempfile

from sentinel.config import Config, CarryCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.state.store import Store
from sentinel.strategy.funding_carry import FundingCarryStrategy, carry_rank

N = 60  # bars of daily history (enough for the momentum lookback)


def _registry(symbols):
    raw = [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
            "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
            "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1}
           for s in symbols]
    return ContractRegistry(raw)


def _series(start, g):
    return [start * (1.0 + g) ** k for k in range(N)]


def _cfg(**kw):
    base = dict(top_k=2, lookback=21, target_gross=2.0)
    base.update(kw)
    return Config(strategy="carry", carry=CarryCfg(**base))


# 6 coins: C5 strongest momentum + lowest funding ... C0 weakest momentum + highest funding
SYMS = [f"E-C{i}-USDT" for i in range(6)]
def _world():
    closes = {f"E-C{i}-USDT": _series(100, 0.002 * (i + 1)) for i in range(6)}   # C5 highest momentum
    funding = {f"E-C{i}-USDT": 0.010 - 0.0022 * i for i in range(6)}             # C0 highest funding, C5 lowest/neg
    prices = {s: 100.0 for s in SYMS}
    return closes, funding, prices


# ---------- ranking ----------
def test_carry_rank_orders_long_first_short_last():
    # cand=(sym, momentum, funding): hi-mom+lo-funding should rank first (best long)
    cand = [("A", 0.30, -0.001), ("B", 0.25, 0.000), ("C", 0.05, 0.004), ("D", 0.01, 0.010)]
    r = carry_rank(cand)
    assert r[0] == "A"            # highest momentum + lowest funding -> best LONG
    assert r[-1] == "D"           # lowest momentum + highest funding -> best SHORT


# ---------- selection ----------
def test_selects_correct_long_and_short_sleeves():
    closes, funding, prices = _world()
    book = FundingCarryStrategy(_cfg(top_k=2)).build_book(closes, funding, prices, _registry(SYMS), 10_000)
    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    shorts = {s for s, p in book.positions.items() if p.target_contracts < 0}
    assert longs == {"E-C5-USDT", "E-C4-USDT"}     # hi momentum + lo funding
    assert shorts == {"E-C0-USDT", "E-C1-USDT"}    # lo momentum + hi funding


# ---------- dollar-neutrality ----------
def test_dollar_neutral_long_equals_short():
    closes, funding, prices = _world()
    book = FundingCarryStrategy(_cfg(top_k=2)).build_book(closes, funding, prices, _registry(SYMS), 10_000)
    assert abs(book.long_notional - book.short_notional) < book.realized_gross * 0.02
    assert abs(book.realized_net) < book.realized_gross * 0.02     # net ~ 0


# ---------- gross scaling ----------
def test_gross_scales_with_target_and_scale():
    closes, funding, prices = _world()
    reg = _registry(SYMS)
    full = FundingCarryStrategy(_cfg(target_gross=2.0)).build_book(closes, funding, prices, reg, 10_000, 1.0)
    half = FundingCarryStrategy(_cfg(target_gross=2.0)).build_book(closes, funding, prices, reg, 10_000, 0.5)
    assert abs(full.realized_gross - 20_000) < 200            # 2.0 * 10000 * 1.0
    assert abs(half.realized_gross - 10_000) < 200            # halved by gross_scale


# ---------- shorts are signed negative ----------
def test_short_positions_signed_negative():
    closes, funding, prices = _world()
    book = FundingCarryStrategy(_cfg(top_k=2)).build_book(closes, funding, prices, _registry(SYMS), 10_000)
    for s in ("E-C0-USDT", "E-C1-USDT"):
        assert book.positions[s].target_contracts < 0
        assert book.positions[s].target_notional < 0
        assert book.positions[s].side == "SHORT"


# ---------- edge: a coin with no funding rate is excluded ----------
def test_missing_funding_excludes_coin():
    closes, funding, prices = _world()
    del funding["E-C5-USDT"]                                   # drop the would-be top long
    book = FundingCarryStrategy(_cfg(top_k=2)).build_book(closes, funding, prices, _registry(SYMS), 10_000)
    assert "E-C5-USDT" not in book.positions                  # excluded for lack of funding
    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    assert "E-C4-USDT" in longs                               # next-best becomes a long


# ---------- edge: too few coins -> empty book ----------
def test_too_few_coins_returns_empty():
    syms = SYMS[:3]
    closes = {s: _series(100, 0.01) for s in syms}
    funding = {s: 0.001 for s in syms}
    prices = {s: 100.0 for s in syms}
    book = FundingCarryStrategy(_cfg(top_k=2)).build_book(closes, funding, prices, _registry(syms), 10_000)
    assert book.positions == {}                               # need >= 2*top_k names


# ---------- trailing-average funding signal (store) ----------
def test_recent_funding_avg_trailing_mean():
    db = tempfile.mktemp(suffix=".db")
    s = Store(db, "/tmp/_carry_test.csv")
    try:
        s.record_funding("paper", {"E-A-USDT": 0.010, "E-B-USDT": -0.002}, {"E-A-USDT": 0.0, "E-B-USDT": 0.0}, ts=1000)
        s.record_funding("paper", {"E-A-USDT": 0.030, "E-B-USDT": 0.002}, {"E-A-USDT": 0.0, "E-B-USDT": 0.0}, ts=2000)
        avg = s.recent_funding_avg("paper", 5)
        assert abs(avg["E-A-USDT"] - 0.020) < 1e-9            # mean(0.010, 0.030)
        assert abs(avg["E-B-USDT"] - 0.000) < 1e-9            # mean(-0.002, 0.002)
        assert s.recent_funding_avg("paper", 1)["E-A-USDT"] == 0.030   # only the latest snapshot
    finally:
        s.close()
        os.path.exists(db) and os.remove(db)
