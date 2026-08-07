"""Universe age gate — keep freshly listed perps out of the book.

CASHCAT was rank-8 by volume ($14M/day) and sailed through the liquidity filter, but it was 24 days
old and its worst day swung 181% (every other name in the book: 7-35%). One short in it cost Carry
$533 — more than the book earned across 110 trades. Volume says a coin is tradable; only age says the
market has had time to price it. These tests pin that a young name is excluded no matter how liquid,
and that a mature one is replaced in rank order rather than the book simply shrinking.
"""
import pytest

import sentinel.engine.marketdata as md
from sentinel.config import UniverseCfg
from sentinel.engine.marketdata import build_universe
from sentinel.exchange.contracts import ContractRegistry


@pytest.fixture(autouse=True)
def _clear_age_cache():
    """The pass-cache is module-level and deliberately lives for the process, so isolate it per test."""
    md._AGE_CACHE.clear()
    yield
    md._AGE_CACHE.clear()

# NEWCOIN is the most liquid name here and the youngest — exactly the CASHCAT shape.
VOLUME = {"BTC": 900e6, "NEWCOIN": 500e6, "ETH": 400e6, "SOL": 200e6, "LINK": 80e6, "TINY": 10e3}
AGE_DAYS = {"BTC": 800, "ETH": 800, "SOL": 800, "LINK": 400, "NEWCOIN": 24, "TINY": 800}


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


def _cfg(**kw):
    base = dict(top_n=3, min_quote_volume_usdt=1e6, min_listing_days=90)
    base.update(kw)
    return UniverseCfg(**base)


def test_young_coin_excluded_however_liquid(fx):
    """NEWCOIN is the 2nd-biggest by volume but 24 days old — it must not make the book."""
    u = build_universe(fx, _registry(list(VOLUME)), _cfg())
    assert "NEWCOIN" not in u
    assert u == ["BTC", "ETH", "SOL"]          # backfilled in rank order, still a full book


def test_book_is_not_left_short_by_the_gate(fx):
    """Dropping a young name must promote the next mature one, not shrink the universe."""
    u = build_universe(fx, _registry(list(VOLUME)), _cfg(top_n=4))
    assert len(u) == 4 and "NEWCOIN" not in u
    assert "LINK" in u                          # promoted into the slot NEWCOIN would have taken


def test_gate_off_restores_previous_behaviour(fx):
    """min_listing_days=0 keeps the old volume-only universe (and skips the age lookups entirely)."""
    u = build_universe(fx, _registry(list(VOLUME)), _cfg(min_listing_days=0))
    assert u == ["BTC", "NEWCOIN", "ETH"]
    assert fx.kline_calls == []


def test_one_unreadable_name_is_skipped_not_guessed(fx, monkeypatch):
    """A single name we can't verify is left out — but the book keeps filling from the rest."""
    def boom(name, interval, bars):
        if name == "ETH":
            raise RuntimeError("history unavailable")
        return [{"idx": i, "close": 1.0} for i in range(min(bars, AGE_DAYS[name]))]
    monkeypatch.setattr(fx, "klines", boom)
    u = build_universe(fx, _registry(list(VOLUME)), _cfg())
    assert "ETH" not in u and len(u) == 3            # backfilled, not shrunk


def test_api_outage_skips_the_gate_instead_of_emptying_the_book(fx, monkeypatch):
    """THE REGRESSION: the gate costs a klines call per candidate, so a rate-limit made every check fail,
    every coin looked 'young', the universe collapsed and Carry went flat with 0 positions. A systemic
    failure must fall back to the volume-only universe, never to an empty one."""
    monkeypatch.setattr(fx, "klines", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429 rate limited")))
    u = build_universe(fx, _registry(list(VOLUME)), _cfg())
    assert len(u) == 3, "an API outage must not empty the universe"
    assert u == ["BTC", "NEWCOIN", "ETH"]            # volume-only fallback for this cycle


def test_a_passed_name_is_not_rechecked(fx):
    """Age only increases, so a name that has passed never needs another lookup — that call volume is
    what made the gate fragile."""
    build_universe(fx, _registry(list(VOLUME)), _cfg())
    n_first = len(fx.kline_calls)
    fx.kline_calls.clear()
    build_universe(fx, _registry(list(VOLUME)), _cfg())
    assert len(fx.kline_calls) < n_first
