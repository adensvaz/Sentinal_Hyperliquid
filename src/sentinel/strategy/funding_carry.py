"""Funding-carry + momentum combo — a market-neutral strategy that harvests perpetual funding.

Validated on 6.5yr of Binance data (scripts/neutral_carry.py): Sharpe ~2.5, maxDD ~32% with the
risk overlay, positive every year 2020-2026, and ~uncorrelated to BOTH the neutral and champion books
(corr +0.07 / market-β -0.06) — a genuine diversifier. The edge is STRUCTURAL: perpetual funding is a
payment from crowded longs to shorts, and it is orthogonal to price momentum.

  Rank every tradable coin by (momentum-rank + funding-rank):
    LONG  the top-K   — high momentum AND low/negative funding
    SHORT the bottom-K — low momentum AND high funding (collect the funding the crowd pays)
  Dollar-neutral, equal-weight each side, gross = target_gross * gross_scale
  (vol-target / drawdown-throttle feed in via gross_scale, exactly like the other books).

Combining momentum with funding is what tames carry's classic tail risk: the momentum tilt stops us
shorting a high-funding coin that is also ripping (the squeeze that wrecks a pure-carry book).

Returns a TargetBook so it reuses the exact same execution / reconciler / store path as the other books.
"""
from __future__ import annotations

from ..exchange.contracts import ContractRegistry
from .momentum_regime import _momentum
from .sentiment_edge import TargetBook, TargetPosition


def carry_rank(cand: list) -> list:
    """cand = [(symbol, momentum, funding), ...] -> symbols ordered best-LONG first (hi-mom + lo-funding).
    Combined cross-sectional rank: momentum ascending + funding descending. The lowest combined score
    is the best SHORT (low momentum + high funding)."""
    syms = [s for s, _, _ in cand]
    mom_rank = {s: i for i, (s, _, _) in enumerate(sorted(cand, key=lambda x: x[1]))}       # 0 = lowest momentum
    fund_rank = {s: i for i, (s, _, _) in enumerate(sorted(cand, key=lambda x: -x[2]))}     # 0 = highest funding
    combo = {s: mom_rank[s] + fund_rank[s] for s in syms}                                   # high = hi-mom + lo-funding
    return sorted(syms, key=lambda s: combo[s], reverse=True)


class FundingCarryStrategy:
    """Market-neutral momentum + funding-carry combo (the validated diversifier book)."""

    def __init__(self, cfg):
        self.c = cfg.carry            # CarryCfg

    def build_book(self, closes_by_symbol: dict, funding_by_symbol: dict, prices: dict,
                   registry: ContractRegistry, equity: float, gross_scale: float = 1.0) -> TargetBook:
        c = self.c
        # 1. eligible names: tradable, priced, with a momentum reading AND a known funding rate
        cand = []
        for s, closes in closes_by_symbol.items():
            if s not in prices or prices[s] <= 0 or not registry.has(s):
                continue
            f = funding_by_symbol.get(s)
            if f is None:
                continue
            m = _momentum(closes, c.lookback)
            if m <= -1e8:                       # not enough history for the lookback
                continue
            cand.append((s, m, float(f)))
        if len(cand) < 2 * c.top_k:             # need enough names for a balanced long & short sleeve
            return TargetBook({}, equity, 0.0)
        # 2. rank and split into the long / short sleeves
        ranked = carry_rank(cand)
        longs, shorts = ranked[: c.top_k], ranked[-c.top_k:]
        score = {s: m for s, m, _ in cand}
        # 3. dollar-neutral, equal-weight each sleeve to the same notional (net ~ 0)
        gross = c.target_gross * equity * gross_scale
        side = gross / 2.0
        positions: dict[str, TargetPosition] = {}
        for s in longs:
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(side / len(longs), prices[s])
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s])
            positions[s] = TargetPosition(s, score[s], "LONG", contracts, realized, prices[s])
        for s in shorts:
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(side / len(shorts), prices[s])
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s])
            positions[s] = TargetPosition(s, score[s], "SHORT", -contracts, -realized, prices[s])
        return TargetBook(positions, equity, gross)
