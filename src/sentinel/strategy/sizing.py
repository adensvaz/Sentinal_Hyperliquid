"""Pure sizing math: scores -> capped sleeve weights -> signed notionals.

Kept free of exchange/config types so it is trivially unit-testable.
"""
from __future__ import annotations

EPS = 1e-12


def normalize(weights: list[float]) -> list[float]:
    w = [max(x, 0.0) for x in weights]
    s = sum(w)
    n = len(w)
    if n == 0:
        return []
    if s <= EPS:
        return [1.0 / n] * n
    return [x / s for x in w]


def capped_weights(raw: list[float], cap: float) -> list[float]:
    """Non-negative weights summing to 1, with no single weight exceeding `cap`.
    If `cap` is infeasibly small (cap < 1/n) it is relaxed to 1/n (equal weight)."""
    n = len(raw)
    if n == 0:
        return []
    cap = max(cap, 1.0 / n)
    w = normalize(raw)
    for _ in range(100):
        over = [i for i, x in enumerate(w) if x > cap + 1e-12]
        if not over:
            break
        excess = sum(w[i] - cap for i in over)
        for i in over:
            w[i] = cap
        under = [i for i in range(n) if i not in over]
        base = sum(w[i] for i in under)
        if not under or base <= EPS:
            break
        for i in under:
            w[i] += excess * (w[i] / base)
    return w


def sleeve_notionals(weights: list[float], sleeve_notional: float, sign: int) -> list[float]:
    """Signed target notional per name (sign=+1 longs, -1 shorts)."""
    return [sign * w * sleeve_notional for w in weights]


def vol_adjust(raw: list[float], vols: list[float], floor: float = 1e-3) -> list[float]:
    """Risk-parity transform: divide each raw weight by the name's volatility so a wild coin gets a
    smaller allocation than a calm one for the same signal strength (equal-RISK, not equal-dollar).
    `floor` guards against a near-zero-vol name capturing the whole sleeve. Returns raw (un-normalized)
    weights; the caller still applies capped_weights() to cap + normalize."""
    return [r / max(v, floor) for r, v in zip(raw, vols)]


def adv_caps(signed_notional: dict, adv_quote: dict, frac: float) -> dict:
    """Per-symbol scale factors (<=1) capping each name's |notional| at `frac` * its ADV (quote volume),
    so the book stays followable and low-impact when copied. `frac` <= 0 disables (all 1.0). A name with
    unknown/zero ADV is left uncapped (we don't have a liquidity estimate to act on)."""
    scales = {s: 1.0 for s in signed_notional}
    if frac <= 0:
        return scales
    for s, n in signed_notional.items():
        cap = frac * adv_quote.get(s, 0.0)
        if cap > 0 and abs(n) > cap:
            scales[s] = cap / abs(n)
    return scales


def net_scales(signed_notional: dict, equity: float, max_net: float) -> dict:
    """Per-symbol scale factors (<=1) that bring the book's |net|/equity within `max_net` by shrinking
    the heavier DOLLAR sleeve. Enforces the dollar-neutral rail after beta-hedging + integer rounding.
    Only ever tightens (never grows a sleeve); returns all-1.0 if already within the rail."""
    scales = {s: 1.0 for s in signed_notional}
    if equity <= EPS or max_net < 0:
        return scales
    L = sum(n for n in signed_notional.values() if n > 0)
    S = sum(-n for n in signed_notional.values() if n < 0)
    net = L - S
    cap = max_net * equity
    if abs(net) <= cap or L <= EPS or S <= EPS:
        return scales
    if net > 0:                       # too long -> shrink the long sleeve
        f = max(0.0, (cap + S) / L)
        for s, n in signed_notional.items():
            if n > 0:
                scales[s] = f
    else:                             # too short -> shrink the short sleeve
        g = max(0.0, (cap + L) / S)
        for s, n in signed_notional.items():
            if n < 0:
                scales[s] = g
    return scales
