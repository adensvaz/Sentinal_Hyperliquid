"""Fetch market data from KoinBay futures and shape it for the signal/strategy.

Handles the live quirks we verified: probe for a working kline interval (1h can return [] while 4h
works), parse the dual idx/close/vol kline fields, and use /fapi/v1/index tagPrice as the mark price.
"""
from __future__ import annotations

import logging

from ..exchange.contracts import ContractRegistry
from ..exchange.futures import KoinbayFutures
from ..signal.base import SymbolData
from ..util.concurrency import pmap

log = logging.getLogger("sentinel")

INTERVAL_CANDIDATES = ("4h", "1h", "30min", "1day")


def pick_base_interval(fx: KoinbayFutures, ref_contract: str, min_bars: int = 10) -> str:
    for itv in INTERVAL_CANDIDATES:
        try:
            rows = fx.klines(ref_contract, itv, 50)
            if isinstance(rows, list) and len(rows) >= min_bars:
                return itv
        except Exception:
            continue
    return "1day"


def _parse_klines(rows: list[dict]):
    idx, closes, vols = [], [], []
    for r in rows:
        try:
            idx.append(int(r["idx"]))
            closes.append(float(r["close"]))
            vols.append(float(r.get("vol", 0) or 0))
        except (KeyError, TypeError, ValueError):
            continue
    order = sorted(range(len(idx)), key=lambda i: idx[i])  # ensure ascending by time
    return [idx[i] for i in order], [closes[i] for i in order], [vols[i] for i in order]


# Symbols already proven old enough. A listing only gets older, so once a name passes the age gate it
# never needs re-checking — this keeps the gate from adding a klines call per candidate every rebalance,
# which is what made it fragile under rate limiting in the first place.
_AGE_CACHE: set[str] = set()


def build_universe(fx: KoinbayFutures, registry: ContractRegistry, ucfg) -> list[str]:
    """Active USDT-margined perps ranked by 24h quote-notional volume, filtered + capped."""
    if ucfg.allowlist:
        candidates = [s for s in ucfg.allowlist if registry.has(s) and registry.get(s).is_active_usdt]
    else:
        deny = set(ucfg.denylist)
        candidates = [s.name for s in registry.active_usdt() if s.name not in deny]

    def _quote_vol(name: str):
        t = fx.ticker(name)
        last = float(t.get("last") or 0)
        vol = float(t.get("vol") or 0)
        return (name, vol * registry.get(name).multiplier * last)  # quote-notional volume

    # fan out the per-symbol ticker calls (no bulk endpoint) — bounded thread pool
    ranked = [r for r in pmap(_quote_vol, candidates, workers=12)
              if r and r[1] >= ucfg.min_quote_volume_usdt]
    ranked.sort(key=lambda x: x[1], reverse=True)

    min_days = int(getattr(ucfg, "min_listing_days", 0) or 0)
    if min_days <= 0:
        universe = [n for n, _ in ranked[: ucfg.top_n]]
        log.info("universe: %d candidates -> %d selected (top by quote volume)", len(candidates), len(universe))
        return universe

    # AGE GATE. Volume ranks a coin; it does not tell you whether the market has had time to price it.
    # A freshly listed perp has no price discovery and a thin float, so it gaps in a way that mature names
    # do not — and a short in one of those is the single most dangerous position this system can hold.
    # Check a buffer beyond top_n so young names are replaced by the next liquid mature ones, not just dropped.
    def _age_ok(name: str):
        if name in _AGE_CACHE:            # a listing only ever gets older — never re-ask once it passes
            return (name, True)
        try:
            bars = fx.klines(name, "1day", min_days + 5)
        except Exception:
            return (name, None)           # UNKNOWN, not "young" — see the degraded-check guard below
        ok = len(bars) >= min_days
        if ok:
            _AGE_CACHE.add(name)
        return (name, ok)

    head = [n for n, _ in ranked[: ucfg.top_n * 2]]
    age = dict(r for r in pmap(_age_ok, head, workers=12) if r)
    # DEGRADED-CHECK GUARD. Failing closed per name is right; failing closed on ALL of them is not.
    # The gate costs one klines call per candidate, so a rate-limit or outage can make every check
    # fail at once — and then a fail-closed gate empties the universe and flattens the whole book on
    # nothing but an API wobble. If most checks came back unknown, skip the gate this cycle instead.
    unknown = sum(1 for v in age.values() if v is None)
    if head and unknown > 0.3 * len(head):
        log.warning("universe: age check unavailable for %d/%d names (API degraded) — skipping the age "
                    "gate this cycle rather than emptying the book", unknown, len(head))
        universe = [n for n, _ in ranked[: ucfg.top_n]]
        log.info("universe: %d candidates -> %d selected (age gate skipped)", len(candidates), len(universe))
        return universe

    universe, young = [], []
    for name, _ in ranked:
        if len(universe) >= ucfg.top_n:
            break
        v = age.get(name)
        if v is None and name in age:
            continue                      # this one name is unknown -> skip it, keep filling from the rest
        if v:
            universe.append(name)
        elif name in age:
            young.append(name)
    if young:
        log.info("universe: excluded %d name(s) under %dd old: %s", len(young), min_days, ", ".join(young[:8]))
    log.info("universe: %d candidates -> %d selected (top by quote volume, min %dd listed)",
             len(candidates), len(universe), min_days)
    return universe


def load_symbol_data(fx: KoinbayFutures, registry: ContractRegistry, symbols: list[str],
                     base_interval: str, history_bars: int):
    """Returns (data, prices, funding) keyed by contractName. Klines + index for each symbol are
    fetched concurrently (no bulk endpoint) — bounded thread pool over the shared httpx pool."""
    def _fetch(sym: str):
        idx, closes, vols = _parse_klines(fx.klines(sym, base_interval, history_bars))
        idxd = fx.index(sym)
        f = float(idxd.get("currentFundRate") or 0.0)
        nf = float(idxd.get("nextFundRate") or f)   # forward funding; falls back to current
        last = float(idxd.get("tagPrice") or (closes[-1] if closes else 0.0))
        if last <= 0:
            return None
        return sym, SymbolData(sym, idx, closes, vols, funding=f, next_funding=nf, last_price=last), last, f

    data: dict[str, SymbolData] = {}
    prices: dict[str, float] = {}
    funding: dict[str, float] = {}
    for r in pmap(_fetch, symbols, workers=12):
        if r:
            sym, sd, last, f = r
            data[sym], prices[sym], funding[sym] = sd, last, f
    return data, prices, funding


def mark_price(fx: KoinbayFutures, sym: str) -> float:
    try:
        return float(fx.index(sym).get("tagPrice") or 0.0)
    except Exception:
        return 0.0
