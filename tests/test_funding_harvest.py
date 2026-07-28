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
    """Counts API calls and can simulate outages, so the tests can pin both cost and failure behavior."""
    funding_interval_hours = 1.0

    def __init__(self):
        self.n_index = 0
        self.n_hist = 0
        self.dead: set[str] = set()            # symbols whose quotes currently fail (simulated API wobble)

    def index(self, s):                        # stable mark == oracle -> zero basis; small positive funding
        self.n_index += 1
        if s in self.dead:
            raise RuntimeError("simulated API failure")
        return {"tagPrice": 100.0, "indexPrice": 100.0, "currentFundRate": 0.00002}

    def funding_history(self, s, days):        # constant positive daily funding -> qualifies (mu>0, huge t-stat)
        self.n_hist += 1
        return [0.0005] * days

    def contracts(self):
        return []

    def reset(self):
        self.n_index = self.n_hist = 0


@pytest.fixture
def fx():
    return _FakeFx()


@pytest.fixture
def engine(monkeypatch, fx):
    monkeypatch.setattr(eng, "make_futures", lambda cfg: fx)
    monkeypatch.setattr(eng, "build_universe", lambda fx_, reg, ucfg: list(SYMS))
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
    f0 = float(engine.store.load_account(engine.mode)["funding_pnl"])
    engine.run_funding_once()
    f1 = float(engine.store.load_account(engine.mode)["funding_pnl"])
    assert f1 > f0                                            # funding kept accruing on the held book


def test_funding_income_is_tracked_separately_from_basis(engine):
    """funding_pnl must hold real funding earned — it used to be merged into realized_pnl, so the UI read $0."""
    engine.run_funding_once()                                 # tick 1 only BUILDS the book (nothing held to accrue on)
    engine.run_funding_once()                                 # tick 2 accrues on it
    acct = engine.store.load_account(engine.mode)
    assert float(acct["funding_pnl"]) > 0.0                   # income is visible, not hidden inside realized
    assert float(acct["realized_pnl"]) == pytest.approx(0.0)  # basis bucket stays separate (flat mark==oracle here)


def test_hold_ticks_skip_the_full_universe_scan(engine, fx):
    """The freeze cause: ~80 API calls every tick. A hold tick must only price the legs it already owns."""
    engine.run_funding_once()                                 # build: scans all 12 + pulls history
    assert fx.n_hist >= len(SYMS)
    fx.reset()
    engine.run_funding_once()                                 # hold tick
    assert fx.n_hist == 0                                     # no trailing-funding scan at all
    assert fx.n_index == engine.cfg.funding.max_coins         # only the 8 held legs priced


def test_transient_api_failure_does_not_liquidate_a_leg(engine, fx):
    """A momentary quote failure must carry the position forward, not silently close it and charge a fee."""
    engine.run_funding_once()
    held = set(engine.store.load_positions(engine.mode))
    fees_before = _fees(engine)
    fx.dead = set(list(held)[:3])                             # 3 legs stop responding this tick
    rep = engine.run_funding_once()
    assert set(engine.store.load_positions(engine.mode)) == held   # every leg still there
    assert _fees(engine) == pytest.approx(fees_before)             # and no phantom exit fee
    assert rep.n_orders == 0


def test_degraded_scan_does_not_rebalance(engine, fx, monkeypatch):
    """If an outage prices only a few coins, the 'best' names are an artifact — hold rather than churn on noise."""
    engine.run_funding_once()
    held = set(engine.store.load_positions(engine.mode))
    fees_before = _fees(engine)
    engine.store.set_meta("funding_last_rebal", 0.0)          # force the rebalance window open
    fx.dead = set(SYMS[3:])                                   # only 3 of 12 coins respond
    engine.run_funding_once()
    assert set(engine.store.load_positions(engine.mode)) == held   # book untouched
    assert _fees(engine) == pytest.approx(fees_before)             # no turnover charged on bad data
