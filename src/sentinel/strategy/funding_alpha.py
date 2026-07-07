"""HL Funding-Alpha — a delta-neutral funding-harvest book engineered for MINIMAL drawdown.

Insight (from the on-venue decomposition): on a venue with real market-clearing funding (Hyperliquid),
the FUNDING leg is near-riskless (isolated Sharpe 10-22, ~0% drawdown); essentially ALL the drawdown of
a carry book is its price/momentum exposure. So this strategy harvests the funding premium and hedges the
price risk away:
  * heavy funding tilt (alpha) with a small momentum term to avoid adverse selection
  * momentum GUARD — never short a coin that's ripping, or long one that's dumping
  * funding-WEIGHTED sizing — deploy more where the premium is richest
  * BTC-beta HEDGE — market-neutral
  * diversified (top_k per side)
  * funding-DISPERSION regime filter — scale gross up when the cross-sectional funding spread is wide,
    down when it compresses (the principled response to funding-edge fade as a venue matures)
(vol-target + drawdown-throttle feed in via gross_scale, exactly like the other books.)

Returns a TargetBook so it reuses the exact execution / reconciler / store path.

HONEST scope: validated on Hyperliquid's OWN ~1.5-2yr history (all that exists) — see
scripts/hl_funding_alpha_bt.py for the split-half / rolling-window robustness. The funding edge fades as
the venue matures; treat headline figures as best-case and rely on live-paper validation.
"""
from __future__ import annotations

import numpy as np

from ..exchange.contracts import ContractRegistry
from .momentum_regime import _momentum
from .sentiment_edge import TargetBook, TargetPosition


def _zscore(d: dict) -> dict:
    v = np.array(list(d.values()), dtype=float)
    if len(v) == 0 or v.std() < 1e-12:
        return {k: 0.0 for k in d}
    z = (v - v.mean()) / v.std()
    return {k: float(z[i]) for i, k in enumerate(d)}


def _beta(closes: list, btc_closes: list, lookback: int) -> float:
    a = np.asarray(closes[-lookback - 1:], dtype=float)
    b = np.asarray(btc_closes[-lookback - 1:], dtype=float)
    if len(a) < 12 or len(a) != len(b) or np.asarray(b).std() < 1e-12:
        return 0.0
    ra, rb = a[1:] / a[:-1] - 1, b[1:] / b[:-1] - 1
    if rb.std() < 1e-9:
        return 0.0
    return float(np.cov(ra, rb)[0, 1] / np.var(rb))


class FundingAlphaStrategy:
    """Delta-neutral funding-harvest book (funding tilt + guard + beta-hedge + regime filter)."""

    def __init__(self, cfg):
        self.c = cfg.funding_alpha
        self._disp_hist: list[float] = []      # rolling funding-dispersion for the regime filter

    def build_book(self, closes_by_symbol: dict, funding_by_symbol: dict, prices: dict,
                   registry: ContractRegistry, equity: float, gross_scale: float = 1.0) -> TargetBook:
        c = self.c
        cand = []
        for s, closes in closes_by_symbol.items():
            if s not in prices or prices[s] <= 0 or not registry.has(s):
                continue
            f = funding_by_symbol.get(s)
            if f is None:
                continue
            m = _momentum(closes, c.lookback)
            if m <= -1e8:                                    # insufficient history for the lookback
                continue
            cand.append((s, m, float(f), closes))
        if len(cand) < 2 * c.top_k:
            return TargetBook({}, equity, 0.0)

        mom = {s: m for s, m, _, _ in cand}
        fnd = {s: f for s, _, f, _ in cand}
        closes_map = {s: cl for s, _, _, cl in cand}

        # --- funding-dispersion regime filter -----------------------------------------------------
        disp = float(np.std(list(fnd.values())))
        self._disp_hist.append(disp)
        if len(self._disp_hist) > c.regime_lookback:
            self._disp_hist = self._disp_hist[-c.regime_lookback:]
        regime_scale = 1.0
        if c.regime_filter and len(self._disp_hist) >= 10:
            base = float(np.median(self._disp_hist))
            if base > 1e-12:
                regime_scale = float(np.clip(disp / base, 0.4, 1.3))

        # --- selection: funding tilt + small momentum term, then guard ----------------------------
        # NB: winsorizing the funding z-score to tame extreme-funding outliers was TESTED and REJECTED
        # (scripts/hl_funding_alpha_bt.py: Sharpe 2.26->1.94, deeper DD, worse 2nd-half OOS). The
        # fat-tailed funding IS the edge; the momentum guard below already handles price-side adverse
        # selection, and the strategy survives realistic blended fees (Sharpe 2.13 @ 3.0bps). Leave raw.
        fz, mz = _zscore(fnd), _zscore(mom)
        score = {s: -c.alpha * fz[s] + (1.0 - c.alpha) * mz[s] for s in mom}   # higher = better LONG
        ranked = sorted(score, key=lambda s: score[s])                         # ascending: best-short .. best-long
        g = c.momentum_guard
        # walk the ranked list taking the top-K per side that PASS the guard (don't long a dumper /
        # short a ripper); this keeps the sleeves full instead of emptying them in a trending market.
        longs = [s for s in reversed(ranked) if g <= 0 or mom[s] > -g][: c.top_k]
        shorts = [s for s in ranked if (g <= 0 or mom[s] < g) and s not in longs][: c.top_k]
        if not longs or not shorts:
            return TargetBook({}, equity, 0.0)

        gross = c.target_gross * equity * gross_scale * regime_scale
        side_notional = gross / 2.0

        def sleeve(names: list, sign: int) -> dict:
            raw = {s: (abs(fnd[s]) if c.fund_weighted else 1.0) for s in names}
            tot = sum(raw.values()) or 1.0
            wt = {s: min(raw[s] / tot, 0.30) for s in names}                   # cap any single name at 30% (diversify)
            t2 = sum(wt.values()) or 1.0
            return {s: sign * wt[s] / t2 for s in names}                       # sleeve sums to +/-1.0

        weights: dict[str, float] = {}
        weights.update(sleeve(longs, +1))
        weights.update(sleeve(shorts, -1))

        # --- BTC-beta hedge: neutralize residual market beta --------------------------------------
        btc = closes_map.get("BTC")
        if c.beta_hedge and btc is not None:
            net_beta = sum(weights[s] * _beta(closes_map[s], btc, c.beta_lookback)
                           for s in list(weights) if s in closes_map)
            weights["BTC"] = weights.get("BTC", 0.0) - net_beta

        positions: dict[str, TargetPosition] = {}
        for s, wfrac in weights.items():
            if abs(wfrac) < 1e-6 or s not in prices or prices[s] <= 0 or not registry.has(s):
                continue
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(wfrac * side_notional, prices[s])
            if contracts == 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s]) * (1 if contracts > 0 else -1)
            side = "LONG" if contracts > 0 else "SHORT"
            positions[s] = TargetPosition(s, score.get(s, 0.0), side, contracts, realized, prices[s])
        return TargetBook(positions, equity, gross)
