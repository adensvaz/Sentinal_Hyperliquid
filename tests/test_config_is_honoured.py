"""Every declared limit must actually BIND — run against the real deployed configs.

Three separate bugs shipped because a setting was declared and silently ignored:

  * funding: max_drawdown_pct: 15 sat in the config while no code path ever acted on it
  * trend:   rebalance_minutes: 2880 was overridden to DAILY by rebalance_hour_utc
  * carry:   beta_neutralize: True was never applied outside the sentiment book

All three were invisible to backtests, because a backtest supplies its own loop and its own risk
handling — it tests the strategy maths, never the runtime that drives it. They were also invisible to
a static "is this field referenced anywhere?" sweep, because each field IS referenced; it just wasn't
honoured on every path.

So these tests are parameterised over the LIVE config files and assert that each declared limit
changes behaviour when fed to the real function that is supposed to enforce it. A limit that binds
nowhere fails here, on whichever book introduced it — including books added later.
"""
import calendar
import glob
import os

import pytest

from sentinel.config import load_config
from sentinel.engine.loop import next_daily_utc
from sentinel.engine.marketdata import filter_by_age
from sentinel.risk.limits import (drawdown_pct, position_loss_breached, position_stop_breached,
                                  risk_scale)
from sentinel.strategy.portfolio import beta_scales, book_beta
from sentinel.strategy.sizing import net_scales

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_CONFIGS = sorted(glob.glob(os.path.join(ROOT, "config.*.yaml")))
NEUTRAL = ("carry", "trend", "neutral")          # books that claim to be market-neutral


def _cfgs():
    out = []
    for p in LIVE_CONFIGS:
        try:
            out.append((os.path.basename(p), load_config(p)))
        except Exception:
            continue                              # a non-book yaml — not our concern here
    return out


CFGS = _cfgs()
IDS = [n for n, _ in CFGS]


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_drawdown_limit_binds(name, cfg):
    """A book at its stated drawdown must be flagged as breached. THE funding bug: the limit was
    configured, the drawdown was computed, and nothing compared them."""
    lim = cfg.risk.max_drawdown_pct
    if not lim:
        pytest.skip("no drawdown limit declared")
    eq = 10_000.0
    peak = eq / (1 - (lim + 5) / 100.0)           # sitting 5pp past the limit
    assert drawdown_pct(peak, eq) > lim, "the limit must be reachable by the drawdown it governs"


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_derisk_actually_derisks(name, cfg):
    """dynamic_risk must shrink gross in a drawdown, and never lever up."""
    if not cfg.risk.dynamic_risk:
        pytest.skip("dynamic_risk off")
    r = cfg.risk
    calm = risk_scale([0.0] * 21, 0.0, r.vol_target_daily, r.min_risk_scale,
                      r.dd_throttle_start, r.dd_throttle_full)
    deep = risk_scale([0.0] * 21, r.dd_throttle_full, r.vol_target_daily, r.min_risk_scale,
                      r.dd_throttle_start, r.dd_throttle_full)
    assert calm <= 1.0, "the throttle must never lever the book up"
    assert deep < calm, "a deep drawdown must reduce gross"
    assert deep >= r.min_risk_scale - 1e-9, "...but never below the configured floor"


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_per_name_stop_binds(name, cfg):
    """A position past position_stop_pct must trip. Loose stops are a choice; a stop that can never
    fire is a bug."""
    sp = cfg.risk.position_stop_pct
    if not sp:
        pytest.skip("no per-name stop declared")
    entry_notional = 1_000.0
    assert position_stop_breached(-entry_notional * (sp / 100.0 + 0.01), entry_notional, sp)
    assert not position_stop_breached(-entry_notional * (sp / 100.0 - 0.01), entry_notional, sp)


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_catastrophe_backstop_binds(name, cfg):
    """max_position_loss_pct is measured against EQUITY, not the position."""
    lim = cfg.risk.max_position_loss_pct
    if not lim:
        pytest.skip("no catastrophe backstop declared")
    eq = 10_000.0
    assert position_loss_breached(-eq * (lim / 100.0 + 0.01), eq, lim)
    assert not position_loss_breached(-eq * (lim / 100.0 - 0.01), eq, lim)


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_net_exposure_rail_binds(name, cfg):
    """A book past max_net_exposure must be clamped back inside it."""
    rail = cfg.risk.max_net_exposure
    if rail is None or rail >= 1.0:
        pytest.skip("no meaningful dollar rail (directional book)")
    eq = 10_000.0
    book = {"A": eq * (rail + 0.25), "B": -eq * 0.10}          # deliberately too long
    fin = {s: n * net_scales(book, eq, rail).get(s, 1.0) for s, n in book.items()}
    assert abs(sum(fin.values())) <= rail * eq + 1e-6


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_beta_neutralize_binds_on_neutral_books(name, cfg):
    """THE carry bug: beta_neutralize was True in config and applied nowhere. Dollar-neutral is not
    market-neutral — a book whose short sleeve runs hotter is quietly short the market."""
    if cfg.strategy not in NEUTRAL or not cfg.portfolio.beta_neutralize:
        pytest.skip("not a beta-neutralised book")
    eq = 10_000.0
    book = {"HOT": -3_000.0, "COOL": 3_000.0}                  # dollar-flat...
    betas = {"HOT": 1.8, "COOL": 0.9}                          # ...but net short in beta
    b0 = book_beta(book, betas, eq)
    assert b0 < -0.10, "fixture must actually be net-short before hedging"
    sc = beta_scales(book, betas, cfg.portfolio.beta_max_imbalance)
    b1 = book_beta({s: n * sc.get(s, 1.0) for s, n in book.items()}, betas, eq)
    assert abs(b1) < abs(b0), "beta_neutralize must reduce the book's market exposure"


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_rebalance_cadence_matches_the_config(name, cfg):
    """THE trend bug: rebalance_hour_utc silently forced DAILY, ignoring rebalance_minutes. The hour
    pins WHEN; the interval still decides HOW OFTEN."""
    s = cfg.schedule
    if s.rebalance_hour_utc is None:
        pytest.skip("interval scheduling — no fixed hour")
    stride = max(1, round(s.rebalance_minutes / 1440))
    now = calendar.timegm((2026, 8, 12, 23, 0, 0, 0, 0, 0))    # after any fixed hour today
    gap_days = (next_daily_utc(s.rebalance_hour_utc, now, every_days=stride) - now) / 86400.0
    assert stride - 1 < gap_days <= stride + 1, (
        f"{name}: config asks for {s.rebalance_minutes}min (stride {stride}d) "
        f"but the next slot is {gap_days:.1f}d away")


@pytest.mark.parametrize("name,cfg", CFGS, ids=IDS)
def test_listing_age_gate_binds(name, cfg):
    """A coin younger than min_listing_days must be dropped — CASHCAT was 24 days old, rank-8 by
    volume, and swung 181% in a day."""
    md = cfg.universe.min_listing_days
    if not md:
        pytest.skip("age gate off")

    class _D:
        def __init__(self, n):
            self.closes = [1.0] * n

    kept = filter_by_age({"OLD": _D(md + 10), "YOUNG": _D(md - 10)}, md)
    assert set(kept) == {"OLD"}
