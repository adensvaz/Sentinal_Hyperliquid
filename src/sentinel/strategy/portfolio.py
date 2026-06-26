"""Portfolio-construction robustness engine + money-flow/risk indicators.

A dollar-neutral book is NOT automatically market-neutral: if the long sleeve carries more BTC-beta than
the short sleeve, the book is secretly long BTC. In crypto, BTC-beta is THE dominant systematic factor,
so neutralizing it is the single biggest robustness gain for a "neutral" strategy.

Tools here:
  compute_betas   — each coin's beta to the reference (BTC) from recent returns
  book_beta       — the book's net beta per $ equity (≈0 == truly market-neutral)  [observability]
  beta_scales     — per-symbol scale factors that zero the book's net beta (capped dollar imbalance)
  dispersion      — cross-sectional return dispersion (the regime tell: low = no edge)  [observability]
"""
from __future__ import annotations

import numpy as np


def _logrets(closes, lookback):
    a = np.asarray([x for x in closes if x and x > 0], dtype=float)
    if len(a) < lookback + 1:
        return None
    return np.diff(np.log(a[-(lookback + 1):]))


def _beta(rc: np.ndarray, rref: np.ndarray) -> float:
    n = min(len(rc), len(rref))
    if n < 5:
        return 1.0
    rc, rref = rc[-n:] - rc[-n:].mean(), rref[-n:] - rref[-n:].mean()
    v = float((rref * rref).mean())
    return float((rc * rref).mean() / v) if v > 0 else 1.0


def compute_betas(closes_by_symbol: dict, ref_symbol: str, lookback: int = 30) -> dict:
    """Beta of each coin's returns vs the reference (BTC). Returns {} if reference history is too short."""
    rref = _logrets(closes_by_symbol.get(ref_symbol, []), lookback)
    if rref is None or (rref * rref).mean() == 0:
        return {}
    out = {}
    for s, c in closes_by_symbol.items():
        rc = _logrets(c, lookback)
        if rc is not None:
            out[s] = _beta(rc, rref)
    return out


def book_beta(signed_notional: dict, betas: dict, equity: float) -> float:
    """Net BTC-beta of the book per $ equity. ~0 == truly market-neutral."""
    if equity <= 0:
        return 0.0
    return sum(n * betas.get(s, 1.0) for s, n in signed_notional.items()) / equity


def beta_scales(signed_notional: dict, betas: dict, max_imbalance: float = 0.15) -> dict:
    """Per-symbol scale factors (<=1) that zero the book's net beta by shrinking the heavier-beta sleeve.
    The shrink is capped at max_imbalance so we never drift too far from dollar-neutral."""
    Lb = sum(n * betas.get(s, 1.0) for s, n in signed_notional.items() if n > 0)
    Sb = sum(-n * betas.get(s, 1.0) for s, n in signed_notional.items() if n < 0)
    scales = {s: 1.0 for s in signed_notional}
    if Lb <= 1e-9 or Sb <= 1e-9:
        return scales
    if Lb > Sb:
        f = max(1.0 - max_imbalance, Sb / Lb)
        for s, n in signed_notional.items():
            if n > 0:
                scales[s] = f
    else:
        f = max(1.0 - max_imbalance, Lb / Sb)
        for s, n in signed_notional.items():
            if n < 0:
                scales[s] = f
    return scales


def demean_by_beta(values: dict, betas: dict) -> dict:
    """Orthogonalize a cross-sectional signal against BTC-beta: subtract the component linearly
    explained by beta so the signal stops loading on the systematic factor at SOURCE. Used on the
    whale overlay (which otherwise injects a directional BTC bet). Returns values unchanged if there
    aren't >=3 overlapping names or beta has no cross-sectional variance."""
    common = [s for s in values if s in betas]
    if len(common) < 3:
        return dict(values)
    v = np.asarray([values[s] for s in common], dtype=float)
    b = np.asarray([betas[s] for s in common], dtype=float)
    bc = b - b.mean()
    var = float((bc * bc).mean())
    if var <= 0:
        return dict(values)
    slope = float(((v - v.mean()) * bc).mean() / var)
    bmean = float(b.mean())
    out = dict(values)
    for s in common:
        out[s] = values[s] - slope * (betas[s] - bmean)
    return out


def dispersion(closes_by_symbol: dict, lookback: int = 5) -> float:
    """Cross-sectional std of recent returns — the regime tell. Low dispersion (everything moving
    together / BTC-beta regime) = little cross-sectional edge."""
    rets = []
    for c in closes_by_symbol.values():
        a = [x for x in c if x and x > 0]
        if len(a) >= lookback + 1 and a[-1 - lookback] > 0:
            rets.append(a[-1] / a[-1 - lookback] - 1.0)
    return float(np.std(rets)) if len(rets) >= 3 else 0.0
