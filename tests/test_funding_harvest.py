"""Funding-harvest turnover control (engine.run_funding_once).

The whole edge of a delta-neutral funding book is that funding accrues every tick whether or not you
trade — so the fix is to HOLD the basket between rebalances instead of re-picking/re-sizing every tick.
These tests pin that behavior: repeated ticks within `rebalance_days` must NOT keep charging fees, and
a name must stay put unless it's past min-hold / falls out of the keep-band / stops paying.
"""
import tempfile

import pytest

from sentinel.config import Config, FundingCfg, StateCfg, UniverseCfg
from sentinel.exchange.contracts import ContractRegistry
import sentinel.engine.engine as eng

SYMS = [f"C{i}" for i in range(12)]           # 12 candidate coins, all paying stable positive funding


def _registry(symbols):
    raw = [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
            "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
            "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1}
           for s in symbols]
    return ContractRegistry(raw)


class _FakeFx:
    funding_interval_hours = 1.0
    def index(self, s):                        # stable mark == oracle -> zero basis; small positive funding
        return {"tagPrice": 100.0, "indexPrice": 100.0, "currentFundRate": 0.00002}
    def funding_history(self, s, days):        # constant positive daily funding -> qualifies (mu>0, huge t-stat)
        return [0.0005] * days
    def contracts(self):
        return []


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(eng, "make_futures", lambda cfg: _FakeFx())
    monkeypatch.setattr(eng, "build_universe", lambda fx, reg, ucfg: list(SYMS))
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    cfg = Config(strategy="funding", capital_usdt=10_000,
                 funding=FundingCfg(rebalance_days=3.0, min_hold_days=3.0, no_trade_band=0.10, max_coins=8),
                 universe=UniverseCfg(top_n=12),
                 state=StateCfg(db_path=db, equity_csv=db + ".csv"))
    e = eng.Engine(cfg, live=False)
    e.registry = _registry(SYMS)
    monkeypatch.setattr(e, "_ensure_market", lambda: None)
    return e


def _fees(e):
    return float((e.store.load_account(e.mode) or {}).get("fees_paid", 0.0))


def test_builds_the_book_on_first_tick(engine):
    rep = engine.run_funding_once()
    assert rep.n_short == engine.cfg.funding.max_coins        # opened a full 8-name short book
    assert rep.net == 0.0                                     # delta-neutral
    assert _fees(engine) > 0.0                                # paid the one-time build fee
    assert engine.store.get_meta("funding_last_rebal") is not None


def test_holds_between_rebalances_and_stops_bleeding_fees(engine):
    """The bug: every hourly tick re-sized/re-picked and paid fees. The fix: ticks within rebalance_days HOLD."""
    engine.run_funding_once()
    build_fee = _fees(engine)
    held = engine.store.load_positions(engine.mode)
    for _ in range(5):                                        # 5 more ticks, all inside the 3-day window
        rep = engine.run_funding_once()
        assert rep.n_orders == 0                              # no turnover -> no orders
    assert _fees(engine) == pytest.approx(build_fee)          # fees did NOT grow tick over tick
    assert engine.store.load_positions(engine.mode).keys() == held.keys()   # same names, untouched


def test_funding_accrues_even_while_holding(engine):
    """Holding must still book funding every tick (that's the income) — only the churn stops."""
    engine.run_funding_once()
    r0 = float(engine.store.load_account(engine.mode)["realized_pnl"])
    engine.run_funding_once()
    r1 = float(engine.store.load_account(engine.mode)["realized_pnl"])
    assert r1 > r0                                            # funding kept accruing on the held book
