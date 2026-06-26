"""Fetch market data from KoinBay futures and shape it for the signal/strategy.

Handles the live quirks we verified: probe for a working kline interval (1h can return [] while 4h
works), parse the dual idx/close/vol kline fields, and use /fapi/v1/index tagPrice as the mark price.
"""
from __future__ import annotations

import logging

from ..exchange.contracts import ContractRegistry
from ..exchange.futures import KoinbayFutures
from ..signal.base import SymbolData

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


def build_universe(fx: KoinbayFutures, registry: ContractRegistry, ucfg) -> list[str]:
    """Active USDT-margined perps ranked by 24h quote-notional volume, filtered + capped."""
    if ucfg.allowlist:
        candidates = [s for s in ucfg.allowlist if registry.has(s) and registry.get(s).is_active_usdt]
    else:
        deny = set(ucfg.denylist)
        candidates = [s.name for s in registry.active_usdt() if s.name not in deny]

    ranked: list[tuple[str, float]] = []
    for name in candidates:
        try:
            t = fx.ticker(name)
            last = float(t.get("last") or 0)
            vol = float(t.get("vol") or 0)
            qv = vol * registry.get(name).multiplier * last  # quote-notional volume
            if qv >= ucfg.min_quote_volume_usdt:
                ranked.append((name, qv))
        except Exception:
            continue
    ranked.sort(key=lambda x: x[1], reverse=True)
    universe = [n for n, _ in ranked[: ucfg.top_n]]
    log.info("universe: %d candidates -> %d selected (top by quote volume)", len(candidates), len(universe))
    return universe


def load_symbol_data(fx: KoinbayFutures, registry: ContractRegistry, symbols: list[str],
                     base_interval: str, history_bars: int):
    """Returns (data, prices, funding) keyed by contractName."""
    data: dict[str, SymbolData] = {}
    prices: dict[str, float] = {}
    funding: dict[str, float] = {}
    for sym in symbols:
        try:
            idx, closes, vols = _parse_klines(fx.klines(sym, base_interval, history_bars))
            idxd = fx.index(sym)
            f = float(idxd.get("currentFundRate") or 0.0)
            nf = float(idxd.get("nextFundRate") or f)   # forward funding; falls back to current
            last = float(idxd.get("tagPrice") or (closes[-1] if closes else 0.0))
            if last <= 0:
                continue
            data[sym] = SymbolData(sym, idx, closes, vols, funding=f, next_funding=nf, last_price=last)
            prices[sym] = last
            funding[sym] = f
        except Exception as e:
            log.warning("data fetch failed for %s: %s", sym, e)
    return data, prices, funding


def mark_price(fx: KoinbayFutures, sym: str) -> float:
    try:
        return float(fx.index(sym).get("tagPrice") or 0.0)
    except Exception:
        return 0.0
