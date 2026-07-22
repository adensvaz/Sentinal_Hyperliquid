"""Grid / market-making strategy — harvest intraday mean-reversion (chop), the honest version of the
edge run by the on-chain market-makers we studied (0xba67…, 0x6a02…).

MECHANIC (per coin, around a persisted `anchor` price):
  - Price DIPS below the anchor  -> go progressively LONG   (buy the dips).
  - Price POPS above the anchor   -> go progressively SHORT  (sell the rips).
  - As price reverts toward the anchor, the target shrinks -> the reconciler unwinds each step for a
    ~`spacing` gain. This IS the grid: a ladder of mean-reversion round-trips, expressed as a snapshot
    target book so it reuses the exact same execution / reconciler / risk / store path as every other book.

STABILITY (why this doesn't martingale-blow-up like a naive grid):
  1. 1x / K sizing — each of the K steps is 1/K of the coin's budget, so a fully-extended coin is at most
     its budget (no leverage). Total gross across all coins is capped at `target_gross` * equity.
  2. Trend filter — the grid only engages when the coin is RANGING (|price/MA - 1| < range_band). In a
     strong trend it stands aside (target flat) and recenters, so it never fights a runaway move.
  3. Hard recenter-stop — if price runs `stop_pct` from the anchor, flatten and re-anchor to current price.
     Caps the tail (the naive-grid killer).

Validated (scripts/grid_regime_bt.py — strict no-same-bar fills, 4.5yr, 8 liquid coins): portfolio
Sharpe ~1.65, ~+3%/month, maxDD ~15%, walk-forward held OOS (1.50 -> 1.86). Fee-sensitive (needs maker
fills) and regime-dependent (best in chop). PAPER first; live is Phase 2.
"""
from __future__ import annotations

from ..exchange.contracts import ContractRegistry
from .sentiment_edge import TargetBook, TargetPosition


class GridStrategy:
    def __init__(self, cfg):
        self.c = cfg.grid            # GridCfg

    def build_book(self, closes_by_symbol: dict, prices: dict, registry: ContractRegistry,
                   equity: float, anchors: dict, gross_scale: float = 1.0) -> TargetBook:
        """Return the snapshot target book. `anchors` (dict[symbol -> float]) is the persisted per-coin
        grid center; it is MUTATED IN PLACE (recentered on stop / trend-exit / first sight) and the engine
        persists it after the call. gross_scale is the risk-overlay multiplier (vol-target / DD-throttle)."""
        c = self.c
        syms = [s for s in prices
                if prices[s] > 0 and registry.has(s) and s in closes_by_symbol]
        if not syms:
            return TargetBook({}, equity, 0.0)

        # Per-coin budget so that if EVERY coin were fully extended at once, total gross ~= target_gross*equity.
        # Typically only a few coins are extended, so realized gross sits well below 1x (conservative).
        budget = max(0.0, c.target_gross * equity * gross_scale) / len(syms)
        unit_notional = budget / max(1, c.levels)
        spacing = c.spacing_pct / 100.0
        stop = c.stop_pct / 100.0

        positions: dict[str, TargetPosition] = {}
        gross = 0.0
        for s in syms:
            p = prices[s]
            a = anchors.get(s)
            if not a or a <= 0:                      # first sight -> anchor here, start flat
                anchors[s] = p
                continue

            # trend filter: only run the grid when the coin is ranging near its MA
            closes = [x for x in closes_by_symbol[s] if x and x > 0]
            ranging = True
            if len(closes) >= c.ma_period:
                ma = sum(closes[-c.ma_period:]) / c.ma_period
                ranging = ma > 0 and abs(p / ma - 1.0) < c.range_band

            dev = p / a - 1.0                        # >0 price above anchor, <0 below
            # hard stop OR trend -> flatten this coin and recenter the grid on the current price
            if abs(dev) >= stop or not ranging:
                anchors[s] = p
                continue

            # levels away from the anchor. dip (dev<0) -> long; pop (dev>0) -> short.
            units = int(round(-dev / spacing)) if spacing > 0 else 0
            units = max(-c.levels, min(c.levels, units))
            if units == 0:                           # within half a step of the anchor -> flat
                continue

            spec = registry.get(s)
            contracts = spec.notional_to_contracts(abs(units) * unit_notional, p)
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, p)
            if units > 0:
                positions[s] = TargetPosition(s, float(units), "LONG", contracts, realized, p)
            else:
                positions[s] = TargetPosition(s, float(units), "SHORT", -contracts, -realized, p)
            gross += realized
        return TargetBook(positions, equity, gross)
