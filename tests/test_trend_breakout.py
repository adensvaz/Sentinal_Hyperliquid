"""Breakout scoring for Trend, and the plumbing that has to hold for it to work at all.

The scoring change is small. The dangerous part is history: the engine sizes its kline window from
ma_period, so a longer donchian_period would leave every name short of data, _breakout would return
-1e9 for all of them, and the book would come back empty with nothing in the logs explaining why.
That is the same shape as the funding drawdown limit that was configured and never enforced, so it
gets a test that runs the real Engine rather than a unit assertion on the scorer.
"""
import tempfile

import pytest

from sentinel.config import (CarryCfg, Config, ExchangeCfg, PortfolioCfg, RiskCfg, StateCfg,
                             TrendCfg, UniverseCfg)
import sentinel.engine.engine as eng
from sentinel.strategy.trend import _breakout, _trend


# ------------------------------------------------------------------ the scorer
def test_breakout_is_bounded_between_minus_half_and_half():
    """Boundedness is the point: an unbounded price/MA ratio lets one vertical candle dominate the
    whole cross-sectional ranking."""
    assert _breakout([1, 2, 3, 4, 5], 5) == pytest.approx(0.5)      # at the high
    assert _breakout([5, 4, 3, 2, 1], 5) == pytest.approx(-0.5)     # at the low
    assert _breakout([1, 5, 3], 3) == pytest.approx(0.0)            # mid-range
    for series in ([1, 100, 2], [3, 3, 3, 900, 1], list(range(1, 60))):
        assert -0.5 <= _breakout(series, min(len(series), 45)) <= 0.5


def test_breakout_ranks_a_ripping_coin_above_a_falling_one():
    up, down = list(range(1, 51)), list(range(50, 0, -1))
    assert _breakout(up, 45) > _breakout(down, 45)


def test_breakout_reports_insufficient_history():
    """It must say 'no data', not silently score 0 — a fabricated middling score would put the
    name straight into the tradeable middle of the ranking."""
    assert _breakout([1, 2, 3], 45) <= -1e8


def test_breakout_handles_a_flat_series_without_dividing_by_zero():
    assert _breakout([7.0] * 50, 45) == 0.0


def test_breakout_ignores_junk_prices():
    assert _breakout([0, None, -1] + list(range(1, 50)), 45) > -1e8


def test_breakout_is_steadier_than_ma_on_a_spike():
    """Turnover is the measured enemy — live Carry paid 13.2%/yr in fees against a flat gross P&L.
    A score that lurches on one candle churns the book."""
    base = [100.0] * 60
    spike = base[:-1] + [180.0]
    d_break = abs(_breakout(spike, 45) - _breakout(base, 45))
    d_ma = abs(_trend(spike, 30) - _trend(base, 30))
    assert d_break < d_ma, f"breakout moved {d_break:.3f} vs MA {d_ma:.3f}"


# ------------------------------------------------------------------ the plumbing
SYMS = [f"C{i}" for i in range(24)]
ALL = ["BTC"] + SYMS


class _Fx:
    funding_interval_hours = 1.0

    def __init__(self, bars=200):
        self.bars = bars

    def _series(self, s):
        import math
        i = ALL.index(s)
        n = max(1, len(ALL) - 1)
        drift = 0.012 * (i / n - 0.5)
        beta = 0.6 + 1.4 * (i / n)
        out, px = [], 100.0
        for k in range(self.bars):
            px *= (1.0 + drift + beta * 0.004 * math.sin(k / 5.0))
            out.append(px)
        return out

    def klines(self, s, interval, limit):
        c = self._series(s)[-limit:]
        return [{"idx": k, "close": v, "open": v, "high": v, "low": v, "vol": 1e6}
                for k, v in enumerate(c)]

    def index(self, s):
        px = self._series(s)[-1]
        return {"tagPrice": px, "indexPrice": px, "currentFundRate": 0.00002,
                "nextFundRate": 0.00002}

    def ticker(self, s):
        return {"last": self._series(s)[-1], "vol": 5e9}

    def contracts(self):
        return [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
                 "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
                 "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1,
                 "openTakerFee": 0.00045, "openMakerFee": 0.00015,
                 "closeTakerFee": 0.00045, "closeMakerFee": 0.00015} for s in ALL]


def _engine(monkeypatch, **trend_over):
    fx = _Fx()
    monkeypatch.setattr(eng, "make_futures", lambda cfg: fx)
    monkeypatch.setattr(eng, "build_universe", lambda fx_, reg, ucfg: list(ALL))
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    tr = dict(top_k=5, ma_period=30, target_gross=2.0)
    tr.update(trend_over)
    cfg = Config(strategy="trend", capital_usdt=10_000,
                 exchange=ExchangeCfg(venue="hyperliquid"),
                 carry=CarryCfg(top_k=5, lookback=21, target_gross=2.0, keep_buffer=0),
                 trend=TrendCfg(**tr),
                 universe=UniverseCfg(top_n=len(ALL), min_listing_days=0, min_quote_volume_usdt=0),
                 portfolio=PortfolioCfg(beta_neutralize=True),
                 risk=RiskCfg(max_drawdown_pct=45.0, max_net_exposure=0.10, position_stop_pct=30.0,
                              dynamic_risk=False, crash_guard=False),
                 state=StateCfg(db_path=db, equity_csv=db + ".csv"))
    return eng.Engine(cfg, live=False)


def test_engine_fetches_enough_history_for_a_long_donchian(monkeypatch):
    """THE regression guard. donchian_period 90 against ma_period 30 must widen the kline window,
    not quietly starve the scorer."""
    e = _engine(monkeypatch, score_mode="breakout", donchian_period=90)
    e._ensure_market()
    assert e.history_bars >= 90, f"history_bars {e.history_bars} cannot feed a 90-day breakout"


def test_ma_mode_does_not_over_fetch(monkeypatch):
    """The widening must be conditional — plain MA books should not start pulling 45 extra bars."""
    e = _engine(monkeypatch, score_mode="ma", ma_period=30)
    e._ensure_market()
    assert e.history_bars < 90


@pytest.mark.parametrize("mode,period", [("ma", 30), ("breakout", 45), ("breakout", 90)])
def test_engine_builds_a_two_sided_book_in_either_mode(monkeypatch, mode, period):
    """An empty book is the failure this whole test file exists to catch."""
    e = _engine(monkeypatch, score_mode=mode, donchian_period=period)
    rep = e.run_once()
    assert rep.n_long > 0 and rep.n_short > 0, f"{mode}/{period} produced no book"


def test_breakout_mode_still_respects_the_drawdown_brake(monkeypatch):
    e = _engine(monkeypatch, score_mode="breakout", donchian_period=45)
    e.run_once()
    e.store.update_peak_equity(e.mode, float(e.cfg.capital_usdt) * 3)
    rep = e.run_once()
    assert rep.n_long == 0 and rep.n_short == 0


def test_default_config_is_unchanged(monkeypatch):
    """Existing deployments must not change behaviour just because the option now exists."""
    from sentinel.config import TrendCfg as TC
    assert TC().score_mode == "ma"
