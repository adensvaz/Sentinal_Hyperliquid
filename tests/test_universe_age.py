"""Listing-age gate — keep freshly listed perps out of the book, without spending API calls to do it.

CASHCAT was rank-8 by volume ($14M/day) and sailed through the liquidity filter, but it was 24 days
old and its worst day swung 181% (every other name in the book: 7-35%). One short in it cost Carry
$533 — more than the book earned across 110 trades. Volume says a coin is tradable; only age says the
market has had time to price it.

The first version of this gate probed each candidate with its own klines call. That burst of ~60 extra
requests exhausted the rate budget right before load_symbol_data needed its own ~60 — and because pmap
swallows a failed fetch, the data behind a 30-name universe silently collapsed under 2*top_k, build_book
returned an empty TargetBook and the reconciler sold the whole book. Carry sat at 0 positions.

So the gate now counts the bars we ALREADY fetch. Same protection, zero extra calls. These tests pin
both halves: the filter works, and building the universe costs no klines calls at all.
"""
import pytest

from sentinel.config import UniverseCfg
from sentinel.engine.marketdata import build_universe, filter_by_age
from sentinel.exchange.contracts import ContractRegistry

# NEWCOIN is the most liquid name here and the youngest — exactly the CASHCAT shape.
VOLUME = {"BTC": 900e6, "NEWCOIN": 500e6, "ETH": 400e6, "SOL": 200e6, "LINK": 80e6, "TINY": 10e3}
AGE_DAYS = {"BTC": 800, "ETH": 800, "SOL": 800, "LINK": 400, "NEWCOIN": 24, "TINY": 800}


class _Data:
    """Stands in for SymbolData — the filter only reads `closes`."""
    def __init__(self, n):
        self.closes = [1.0] * n


def _registry(symbols):
    raw = [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 1.0,
            "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
            "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1}
           for s in symbols]
    return ContractRegistry(raw)


class _Fx:
    def __init__(self):
        self.kline_calls = []

    def ticker(self, name):
        return {"last": 1.0, "vol": VOLUME[name]}

    def klines(self, name, interval, bars):
        self.kline_calls.append(name)
        return [{"idx": i, "close": 1.0} for i in range(min(bars, AGE_DAYS[name]))]


@pytest.fixture
def fx():
    return _Fx()


# ── the gate itself ────────────────────────────────────────────────────────────

def test_young_coin_is_dropped_however_liquid():
    data = {s: _Data(AGE_DAYS[s]) for s in VOLUME}
    kept = filter_by_age(data, 90)
    assert "NEWCOIN" not in kept                 # 24 days old — out, despite being 2nd by volume
    assert set(kept) == {"BTC", "ETH", "SOL", "LINK", "TINY"}


def test_gate_off_is_a_no_op():
    data = {s: _Data(AGE_DAYS[s]) for s in VOLUME}
    assert filter_by_age(data, 0) == data


def test_boundary_is_inclusive():
    """Exactly min_listing_days of history counts as seasoned enough."""
    assert set(filter_by_age({"A": _Data(90), "B": _Data(89)}, 90)) == {"A"}


def test_missing_history_is_treated_as_young():
    """A symbol whose bars never arrived cannot prove its age, so it sits this cycle out. Safe here
    only because the caller holds its existing book when the fetch is broadly degraded."""
    assert filter_by_age({"A": _Data(0)}, 90) == {}


# ── the reason it moved: building the universe must cost no klines calls ───────

def test_build_universe_makes_no_kline_calls(fx):
    """THE REGRESSION. A per-candidate age probe blew the rate budget and emptied the book. Ranking
    the universe must stay pure volume — the age check happens later, on bars already fetched."""
    u = build_universe(fx, _registry(list(VOLUME)), UniverseCfg(top_n=3, min_quote_volume_usdt=1e6,
                                                               min_listing_days=90))
    assert fx.kline_calls == [], "the age gate must not cost an API call per candidate"
    assert u == ["BTC", "NEWCOIN", "ETH"]        # volume order; NEWCOIN is filtered downstream


def test_universe_is_not_shrunk_by_the_gate(fx):
    """The universe stays full — dropping a young name later must not leave the book short, because
    the extra names are already in the list."""
    u = build_universe(fx, _registry(list(VOLUME)), UniverseCfg(top_n=5, min_quote_volume_usdt=1e6,
                                                               min_listing_days=90))
    assert len(u) == 5
    data = {s: _Data(AGE_DAYS[s]) for s in u}
    assert len(filter_by_age(data, 90)) == 4     # NEWCOIN out, four mature names remain


# ── rebalance cadence: a fixed hour must not silently override rebalance_minutes ──
# Trend asked for 2880 minutes (2 days) — its config even notes the backtest at Sharpe 0.96 vs 0.79
# for daily — but setting rebalance_hour_utc made `fixed` true and the loop rebalanced DAILY, ignoring
# the interval. A 2-day trend signal was being re-ranked twice as often as designed.

import calendar as _cal
import time as _time

from sentinel.engine.loop import next_daily_utc

_NOW = _cal.timegm((2026, 8, 12, 15, 0, 0, 0, 0, 0))      # just past 14:00 UTC


def _utc(ts):
    return _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(ts))


def test_daily_stride_is_the_next_day():
    assert _utc(next_daily_utc(14, _NOW, every_days=1)) == "2026-08-13 14:00"


def test_two_day_stride_skips_a_day():
    assert _utc(next_daily_utc(14, _NOW, every_days=2)) == "2026-08-14 14:00"


def test_stride_defaults_to_daily():
    assert next_daily_utc(14, _NOW) == next_daily_utc(14, _NOW, every_days=1)


def test_hour_is_still_honoured_before_the_cutoff():
    """Before the hour passes today, the next slot is today — stride only governs skipped days."""
    early = _cal.timegm((2026, 8, 12, 9, 0, 0, 0, 0, 0))
    assert _utc(next_daily_utc(14, early, every_days=2)) == "2026-08-12 14:00"
