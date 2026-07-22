"""Resting-limit grid book (grid_book) — deterministic unit tests (no network). Verifies the core
market-making mechanic: resting buys fill on the dip, take-profits book the spacing on the bounce,
the hard stop flattens + recenters, the trend filter stands aside, and the strict no-same-bar rule."""
from sentinel.strategy import grid_book as gb

P = dict(levels=6, spacing=0.012, stop=0.10, cost=0.0005, unit_notional=1000.0)


def test_dip_fills_a_resting_buy():
    st = gb.new_state(100.0)
    # bar dips to 98.5 (below the first buy level 100*(1-0.012)=98.8) -> one long lot opens
    r = gb.tick(st, high=100.0, low=98.5, close=99.0, ranging=True, **P)
    assert len(st["longs"]) == 1
    assert r["net_units"] == 1
    assert any(f["kind"] == "entry" and f["side"] == "BUY" for f in r["fills"])
    assert r["realized"] == 0.0        # entry alone books nothing


def test_take_profit_books_the_spacing():
    st = gb.new_state(100.0)
    gb.tick(st, high=100.0, low=98.5, close=99.0, ranging=True, **P)   # buy lot at 98.8
    entry = st["longs"][0]
    # a LATER bar rises through entry*(1+spacing) -> the lot sells for ~spacing profit
    r = gb.tick(st, high=entry * 1.02, low=99.0, close=entry * 1.02, ranging=True, **P)
    assert r["realized"] > 0
    assert r["closed"] and r["closed"][0]["side"] == "LONG" and r["closed"][0]["pnl"] > 0
    # net long exposure is smaller than the pre-exit peak (the take-profit lot closed)
    assert st["longs"].count(entry) == 0


def test_pop_opens_a_short():
    st = gb.new_state(100.0)
    r = gb.tick(st, high=101.5, low=100.0, close=101.0, ranging=True, **P)   # above 100*(1+0.012)=101.2
    assert len(st["shorts"]) == 1
    assert r["net_units"] == -1


def test_strict_no_same_bar_roundtrip():
    st = gb.new_state(100.0)
    # a single wild bar spans both a buy level AND that lot's take-profit — must NOT round-trip same bar
    r = gb.tick(st, high=103.0, low=97.0, close=100.0, ranging=True, **P)
    assert r["realized"] == 0.0        # entries only; no exit on the entering bar
    assert not r["closed"]


def test_hard_stop_flattens_and_recenters():
    st = gb.new_state(100.0)
    gb.tick(st, high=100.0, low=98.5, close=99.0, ranging=True, **P)   # open a long
    assert st["longs"]
    # price craters through the 10% stop -> flatten + re-anchor to the close
    r = gb.tick(st, high=99.0, low=89.0, close=90.0, ranging=True, **P)
    assert st["longs"] == []
    assert r["realized"] < 0           # the stop realizes a loss
    assert abs(st["anchor"] - 90.0) < 1e-6


def test_trend_filter_blocks_new_entries():
    st = gb.new_state(100.0)
    # a dip that WOULD fill a buy, but ranging=False (coin is trending) -> no new entry
    r = gb.tick(st, high=100.0, low=97.0, close=98.0, ranging=False, **P)
    assert st["longs"] == []
    assert r["net_units"] == 0


def test_gross_capped_at_K_levels():
    st = gb.new_state(100.0)
    # a huge dip within the stop that trades through all 6 levels at once
    gb.tick(st, high=100.0, low=100.0 * (1 - 6 * P["spacing"]) - 0.01, close=94.0, ranging=True, **P)
    assert len(st["longs"]) == P["levels"]        # never exceeds K
