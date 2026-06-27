"""Champion strategy — directional trend/momentum with a market-regime brake.

Exhaustively validated on 5.5yr survivorship-free Binance data (Sharpe ~1.1-1.2 vs buy-hold 0.35,
walk-forward robust across both halves). Unlike the market-neutral book, this KEEPS crypto's beta —
which the long backtest proved is where the durable edge lives — but gates it with a regime filter:

  1. REGIME: only hold risk if BTC is above its `regime_ma`-day average (else go to cash).
  2. SELECT: rank coins by `lookback`-day momentum; keep the top-K that are ALSO above their own
     `trend_ma`-day average (dual confirmation: relative strength + absolute uptrend).
  3. SIZE: equal-weight, long-only, gross = target_gross * gross_scale (vol-target/DD-throttle feed in
     via gross_scale). No shorts, no beta-neutralization — the market exposure is the point.

Returns a TargetBook so it reuses the exact same execution / reconciler / store path as the neutral book.
"""
from __future__ import annotations

from ..exchange.contracts import ContractRegistry
from .sentiment_edge import TargetBook, TargetPosition


def _above_ma(closes: list, ma_bars: int) -> bool:
    a = [x for x in closes if x and x > 0]
    if len(a) < ma_bars + 1:
        return False
    return a[-1] > sum(a[-ma_bars:]) / ma_bars


def _momentum(closes: list, lookback: int) -> float:
    a = [x for x in closes if x and x > 0]
    if len(a) < lookback + 1 or a[-1 - lookback] <= 0:
        return -1e9
    return a[-1] / a[-1 - lookback] - 1.0


def regime_on(btc_closes: list, regime_ma: int) -> bool:
    """Risk-on when BTC is above its regime_ma-day average."""
    return _above_ma(btc_closes, regime_ma)


class MomentumRegimeStrategy:
    """Long-only top-K momentum with a BTC-regime brake (the backtest champion)."""

    def __init__(self, cfg):
        self.c = cfg.champion           # ChampionCfg
        self.ref = "E-BTC-USDT"

    def build_book(self, closes_by_symbol: dict, prices: dict, registry: ContractRegistry,
                   equity: float, gross_scale: float = 1.0) -> TargetBook:
        c = self.c
        gross = c.target_gross * equity * gross_scale
        # 1. regime brake — flat (cash) when BTC is below its long average
        btc = closes_by_symbol.get(self.ref)
        if btc is None or not regime_on(btc, c.regime_ma):
            return TargetBook({}, equity, 0.0)
        # 2. rank by momentum, keep top-K that are also in an absolute uptrend (dual confirmation)
        cand = []
        for s, closes in closes_by_symbol.items():
            if s not in prices or prices[s] <= 0 or not registry.has(s):
                continue
            if c.dual_confirm and not _above_ma(closes, c.trend_ma):
                continue
            m = _momentum(closes, c.lookback)
            if m > -1e8:
                cand.append((s, m))
        cand.sort(key=lambda x: x[1], reverse=True)
        picks = [(s, m) for s, m in cand[: c.top_k] if m > 0]   # only positive momentum
        if not picks:
            return TargetBook({}, equity, 0.0)
        # 3. equal-weight, long-only
        per_name = gross / len(picks)
        positions: dict[str, TargetPosition] = {}
        for s, m in picks:
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(per_name, prices[s])
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s])
            positions[s] = TargetPosition(s, m, "LONG", contracts, realized, prices[s])
        return TargetBook(positions, equity, gross)
