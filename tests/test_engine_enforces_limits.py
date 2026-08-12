"""The engine must actually CALL the limits it declares — not merely be able to.

test_config_is_honoured.py proves each limit CAN bind when handed to its function. That is only half
the job, and it is the half that would have missed the funding book's drawdown bug: max_drawdown_pct
was reachable, drawdown_pct computed it correctly, and no code path ever compared the two.

This runs the real Engine.run_once against a fake exchange and asserts the limits change what the book
does. Every book is covered, so a limit that is enforced for one strategy and quietly skipped for
another — which is exactly how carry lost its beta hedge and trend lost its cadence — fails here.
"""
import tempfile

import pytest

from sentinel.config import (CarryCfg, Config, ExchangeCfg, PortfolioCfg, RiskCfg, StateCfg,
                             TrendCfg, UniverseCfg)
import sentinel.engine.engine as eng
from sentinel.exchange.contracts import ContractRegistry

SYMS = [f"C{i}" for i in range(24)]
BTC = "BTC"
ALL = [BTC] + SYMS


def _registry(symbols):
    raw = [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
            "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
            "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1}
           for s in symbols]
    return ContractRegistry(raw)


class _Fx:
    """Deterministic market: each coin trends at its own rate, so momentum/trend ranks are stable."""
    funding_interval_hours = 1.0

    def __init__(self, bars=140):
        self.bars = bars

    def _series(self, s):
        # A spread of up/down trends PLUS a shared market wiggle: constant returns would give BTC zero
        # variance, compute_betas would return {}, and the beta assertions would vacuously pass.
        import math
        i = ALL.index(s)
        n = max(1, len(ALL) - 1)
        drift = 0.012 * (i / n - 0.5)                      # -0.6%..+0.6% a bar: genuinely two-sided
        beta = 0.6 + 1.4 * (i / n)                         # 0.6..2.0, so the sleeves differ in beta
        out, px = [], 100.0
        for k in range(self.bars):
            mkt = 0.004 * math.sin(k / 5.0)                # shared factor, small enough not to swamp drift
            px *= (1.0 + drift + beta * mkt)
            out.append(px)
        return out

    def klines(self, s, interval, limit):
        c = self._series(s)[-limit:]
        return [{"idx": k, "close": v, "open": v, "high": v, "low": v, "vol": 1e6} for k, v in enumerate(c)]

    def index(self, s):
        px = self._series(s)[-1]
        return {"tagPrice": px, "indexPrice": px, "currentFundRate": 0.00002, "nextFundRate": 0.00002}

    def ticker(self, s):
        return {"last": self._series(s)[-1], "vol": 5e9}

    def contracts(self):
        # the engine builds its own registry from this, and _ensure_market also constructs the signal —
        # pre-setting registry from the test would short-circuit that and leave signal None
        return [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
                 "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
                 "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1,
                 "openTakerFee": 0.00045, "openMakerFee": 0.00015,
                 "closeTakerFee": 0.00045, "closeMakerFee": 0.00015}
                for s in ALL]


def _engine(monkeypatch, strategy, **risk_over):
    fx = _Fx()
    monkeypatch.setattr(eng, "make_futures", lambda cfg: fx)
    monkeypatch.setattr(eng, "build_universe", lambda fx_, reg, ucfg: list(ALL))
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    risk = dict(max_drawdown_pct=45.0, max_net_exposure=0.10, position_stop_pct=30.0,
                max_position_loss_pct=12.0, dynamic_risk=False, crash_guard=False, pause_hours=24.0)
    risk.update(risk_over)
    cfg = Config(strategy=strategy, capital_usdt=10_000,
                 # venue matters even with a fake fx: it decides btc_sym, and compute_betas
                 # returns {} when it cannot find its reference — which silently skips the hedge
                 exchange=ExchangeCfg(venue="hyperliquid"),
                 carry=CarryCfg(top_k=5, lookback=21, target_gross=2.0, keep_buffer=0),
                 trend=TrendCfg(top_k=5, ma_period=30, target_gross=2.0),
                 universe=UniverseCfg(top_n=len(ALL), min_listing_days=0, min_quote_volume_usdt=0),
                 portfolio=PortfolioCfg(beta_neutralize=True),
                 risk=RiskCfg(**risk),
                 state=StateCfg(db_path=db, equity_csv=db + ".csv"))
    return eng.Engine(cfg, live=False)      # let _ensure_market build registry AND signal


BOOKS = ["carry", "trend"]        # the dollar-neutral books that share this pipeline


@pytest.mark.parametrize("strategy", BOOKS)
def test_engine_builds_a_two_sided_book(strategy, monkeypatch):
    """Baseline — without this the other assertions could pass on an empty book."""
    e = _engine(monkeypatch, strategy)
    rep = e.run_once()
    assert rep.n_long > 0 and rep.n_short > 0, f"{strategy} built no book; the rest proves nothing"


@pytest.mark.parametrize("strategy", BOOKS)
def test_engine_enforces_the_drawdown_kill_switch(strategy, monkeypatch):
    """THE funding-book bug generalised: a configured limit that nothing compares against.
    Past max_drawdown_pct the engine must flatten and pause, on every book."""
    e = _engine(monkeypatch, strategy)
    e.run_once()
    assert e.store.load_positions(e.mode), "expected a book before the brake is tested"
    e.store.update_peak_equity(e.mode, float(e.cfg.capital_usdt) * 3)   # force a deep drawdown
    rep = e.run_once()
    assert rep.n_long == 0 and rep.n_short == 0, f"{strategy} kept trading past its drawdown limit"
    assert e.store.paused_until(e.mode) > 0, f"{strategy} did not enter its cooldown"


@pytest.mark.parametrize("strategy", BOOKS)
def test_engine_respects_the_pause(strategy, monkeypatch):
    """A paused book must stay flat rather than quietly re-opening on the next cycle."""
    import time as _t
    e = _engine(monkeypatch, strategy)
    e.run_once()
    e.store.set_paused_until(e.mode, _t.time() + 3600)
    rep = e.run_once()
    assert rep.n_long == 0 and rep.n_short == 0


@pytest.mark.parametrize("strategy", BOOKS)
def test_engine_holds_the_net_exposure_rail(strategy, monkeypatch):
    """Dollar-neutral books must land inside max_net_exposure once the book is built — the beta hedge
    shrinks a sleeve, so this is what stops the hedge itself from breaching the rail."""
    e = _engine(monkeypatch, strategy)
    rep = e.run_once()
    # the engine itself tolerates 1pp past the rail for integer-contract rounding (it warns beyond
    # that), so hold this assertion to the same standard rather than a tighter one it never promised
    assert abs(rep.net) <= (e.cfg.risk.max_net_exposure + 0.01) * rep.equity, (
        f"{strategy} net {rep.net:+.0f} outside the "
        f"{e.cfg.risk.max_net_exposure:.0%} rail on equity {rep.equity:,.0f}")


@pytest.mark.parametrize("strategy", BOOKS)
def test_engine_records_the_book_beta(strategy, monkeypatch):
    """THE carry bug: beta_neutralize was True and applied nowhere. If the hedge runs it writes
    book_beta; a missing key means the hedge never executed for this strategy."""
    e = _engine(monkeypatch, strategy)
    e.run_once()
    assert e.store.get_meta("book_beta") is not None, (
        f"{strategy} never ran the beta hedge despite beta_neutralize=True")


@pytest.mark.parametrize("strategy", BOOKS)
def test_engine_stays_within_gross_leverage(strategy, monkeypatch):
    e = _engine(monkeypatch, strategy)
    rep = e.run_once()
    assert rep.gross <= e.cfg.risk.max_gross_leverage * rep.equity + 1.0
