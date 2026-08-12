"""Tests for the funding-carry + momentum combo strategy (market-neutral diversifier book)."""
import os
import tempfile

import pytest

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


# ---------- hysteresis: don't churn a name that is merely hovering at the cut-off ----------
# Trade-level evidence: every dollar of loss came from positions held <=2 days — names flickering
# across the top_k boundary, paying a round trip each time. keep_buffer keeps a HELD name until it
# falls out of top_k+buffer, while new entries still require top_k.

def test_held_name_just_outside_topk_is_kept():
    closes, funding, prices = _world()
    reg = _registry(SYMS)
    cfg = _cfg(top_k=2, keep_buffer=2)
    # C3 ranks 3rd (outside top_k=2) — but we already hold it, so the band should keep it
    held = {"E-C3-USDT": 100}
    book = FundingCarryStrategy(cfg).build_book(closes, funding, prices, reg, 10_000, held=held)
    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    assert "E-C3-USDT" in longs, "a held name inside the keep-band must not be churned out"
    assert len(longs) == 2                       # sleeve size unchanged — it took a slot, not an extra one


def test_unheld_name_outside_topk_is_not_bought():
    """The band only KEEPS; entries still require a genuine top_k rank."""
    closes, funding, prices = _world()
    book = FundingCarryStrategy(_cfg(top_k=2, keep_buffer=2)).build_book(
        closes, funding, prices, _registry(SYMS), 10_000, held={})
    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    assert longs == {"E-C5-USDT", "E-C4-USDT"}   # unchanged from the no-buffer selection


def test_name_outside_the_band_is_still_dropped():
    """Hysteresis must widen the exit, not remove it — a name well outside the band still goes."""
    closes, funding, prices = _world()
    held = {"E-C0-USDT": 100}                    # ranks last of 6; outside top_k(2)+buffer(2)
    book = FundingCarryStrategy(_cfg(top_k=2, keep_buffer=2)).build_book(
        closes, funding, prices, _registry(SYMS), 10_000, held=held)
    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    assert "E-C0-USDT" not in longs


def test_keep_buffer_zero_restores_old_behaviour():
    closes, funding, prices = _world()
    held = {"E-C3-USDT": 100}
    book = FundingCarryStrategy(_cfg(top_k=2, keep_buffer=0)).build_book(
        closes, funding, prices, _registry(SYMS), 10_000, held=held)
    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    assert longs == {"E-C5-USDT", "E-C4-USDT"}


# ---------- dollar-neutral is not market-neutral ----------
# Carry shorts HIGH-FUNDING coins, and high funding means crowded longs — the hot, high-beta names.
# So its short sleeve carries systematically more beta than its long sleeve and the book sits quietly
# net-SHORT the market. Measured on the live book: longs averaged beta 1.14 vs shorts 1.38, a $348
# dollar-net book carrying -$1,169 of beta (~12% of equity short). A market rally then shows up as
# losses on a book that is supposed to be indifferent to direction.

from sentinel.strategy.portfolio import beta_scales, book_beta
from sentinel.strategy.sizing import net_scales

LIVE_BOOK = {"ZEC": -897, "BNB": 897, "ETH": 897, "ADA": 896, "UNI": 895, "NEAR": -889,
             "ENA": 880, "INJ": -863, "XPL": 861, "CRV": 856, "MON": -828, "LIT": 828,
             "LINK": -826, "SUI": -801, "FARTCOIN": -785, "TAO": -773}
LIVE_BETA = {"ZEC": 1.91, "BNB": 0.89, "ETH": 1.30, "ADA": 1.53, "UNI": 1.09, "NEAR": 0.96,
             "ENA": 0.69, "INJ": 1.12, "XPL": 1.47, "CRV": 1.16, "MON": 1.24, "LIT": 1.02,
             "LINK": 1.21, "SUI": 1.26, "FARTCOIN": 1.85, "TAO": 1.50}
EQ, RAIL = 9923.41, 0.10


def _hedged(book, betas, equity, rail):
    """The engine's order: beta-hedge, then clamp back inside the dollar rail."""
    heavier = max(sum(n for n in book.values() if n > 0), sum(-n for n in book.values() if n < 0))
    sc = beta_scales(book, betas, min(0.15, rail * equity / heavier))
    mid = {s: n * sc.get(s, 1.0) for s in book for n in [book[s]]}
    cl = net_scales(mid, equity, rail)
    return {s: n * cl.get(s, 1.0) for s, n in mid.items()}


def test_live_book_was_net_short_despite_looking_dollar_neutral():
    """The bug this fixes: +$348 dollar-net reads as neutral while carrying -12% of equity in beta."""
    assert abs(sum(LIVE_BOOK.values())) <= RAIL * EQ          # dollar-neutral by the book's own rail
    assert book_beta(LIVE_BOOK, LIVE_BETA, EQ) < -0.10        # ...yet meaningfully short the market


def test_hedge_removes_most_of_the_market_exposure():
    before = book_beta(LIVE_BOOK, LIVE_BETA, EQ)
    after = book_beta(_hedged(LIVE_BOOK, LIVE_BETA, EQ, RAIL), LIVE_BETA, EQ)
    assert abs(after) < abs(before) * 0.3, "the hedge must remove most of the net beta"


def test_hedge_never_breaches_the_dollar_rail():
    """The hedge shrinks one sleeve, so it OPENS a dollar imbalance — it must be followed by the
    clamp. Without it the book would sit ~12% net long, past its own max_net_exposure."""
    fin = _hedged(LIVE_BOOK, LIVE_BETA, EQ, RAIL)
    assert abs(sum(fin.values())) <= RAIL * EQ + 1e-6


def test_hedge_is_a_no_op_on_an_already_neutral_book():
    book = {"A": 1000, "B": -1000}
    betas = {"A": 1.0, "B": 1.0}
    fin = _hedged(book, betas, EQ, RAIL)
    assert fin == pytest.approx(book, rel=1e-9)
