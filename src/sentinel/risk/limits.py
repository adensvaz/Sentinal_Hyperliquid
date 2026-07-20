"""Risk controls: leverage cap, net-exposure check, drawdown kill-switch, funding filter.

Pure functions where possible so they're easy to test and to reason about.
"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-12


def leverage_scale(gross_notional: float, equity: float, max_gross_leverage: float) -> float:
    """Factor in (0, 1] to multiply target sizes by so gross/equity <= max_gross_leverage.
    Returns 1.0 if already within the cap."""
    if equity <= EPS:
        return 0.0
    cap_notional = max_gross_leverage * equity
    if gross_notional <= cap_notional or gross_notional <= EPS:
        return 1.0
    return cap_notional / gross_notional


def net_exposure_ratio(net_notional: float, equity: float) -> float:
    if equity <= EPS:
        return 0.0
    return abs(net_notional) / equity


def net_within_tolerance(net_notional: float, equity: float, tol: float) -> bool:
    return net_exposure_ratio(net_notional, equity) <= tol


def drawdown_pct(peak_equity: float, equity: float) -> float:
    if peak_equity <= EPS:
        return 0.0
    return max(0.0, (peak_equity - equity) / peak_equity * 100.0)


def drawdown_breached(peak_equity: float, equity: float, max_drawdown_pct: float) -> bool:
    return drawdown_pct(peak_equity, equity) > max_drawdown_pct


def risk_scale(recent_returns: list, drawdown_frac: float, target_vol: float,
               min_scale: float, dd_start: float, dd_full: float) -> float:
    """Dynamic de-risking factor in [min_scale, 1.0] to multiply gross leverage by.
    Combines (a) volatility targeting — cut gross when recent per-period return vol exceeds target —
    and (b) a drawdown throttle — linearly cut gross from 1.0 at dd_start to ~0 at dd_full.
    Only ever de-risks (capped at 1.0); never levers up."""
    import statistics
    scale = 1.0
    if recent_returns and len(recent_returns) >= 2 and target_vol > 0:
        vol = statistics.pstdev(recent_returns)
        if vol > target_vol:
            scale = min(scale, target_vol / vol)
    if dd_full > dd_start and drawdown_frac > dd_start:
        t = min(1.0, (drawdown_frac - dd_start) / (dd_full - dd_start))
        scale *= (1.0 - t)
    return max(min_scale, min(1.0, scale))


def crash_guard(ref_closes: list, dd_bars: int, bounce_bars: int,
                dd_trigger: float, bounce_trigger: float, floor: float) -> float:
    """Momentum-crash de-risk factor in [floor, 1.0] from the market reference (BTC) price path.

    Cross-sectional momentum has a fat LEFT tail: after a deep market selloff, a violent rebound makes
    recent losers (our shorts) rip while recent winners (our longs) lag — the neutral book gets run over.
    The setup is detectable: a large drawdown from a recent high (bear context) *and* a sharp short-window
    bounce off the low (the snapback). When both fire, cut gross toward `floor`, scaled by bounce severity.
    Returns 1.0 outside the regime. Windows are passed in BARS so the caller stays interval-agnostic."""
    a = [x for x in ref_closes if x and x > 0]
    if len(a) < max(dd_bars, bounce_bars) + 1:
        return 1.0
    window = a[-(dd_bars + 1):]
    peak = max(window)
    dd = (peak - a[-1]) / peak if peak > 0 else 0.0          # how far below the recent high
    bounce = a[-1] / a[-1 - bounce_bars] - 1.0               # short-window snapback
    if dd < dd_trigger or bounce < bounce_trigger:
        return 1.0
    over = (bounce - bounce_trigger) / max(bounce_trigger, EPS)   # how violent beyond the trigger
    return max(floor, 1.0 - (1.0 - floor) * min(1.0, 0.5 + 0.5 * over))


def dispersion_scale(disp: float, ref: float, min_scale: float) -> float:
    """Gross de-risk factor in [min_scale, 1.0] from cross-sectional dispersion vs its trailing norm.
    Low dispersion = coins moving together = no winner-vs-loser edge -> shrink the book. Never levers up
    (caps at 1.0). Returns 1.0 if there's no reference yet (warmup)."""
    if ref <= 0 or disp <= 0:
        return 1.0
    return max(min_scale, min(1.0, disp / ref))


def position_loss_breached(pnl: float, equity: float, max_loss_pct: float) -> bool:
    """True if a single position's (signed) PnL is a loss exceeding max_loss_pct of equity.
    This is the CATASTROPHE backstop (a single name melting a big % of the whole book)."""
    if equity <= 0 or max_loss_pct <= 0:
        return False
    return pnl < -(max_loss_pct / 100.0) * equity


def position_stop_breached(pnl: float, entry_notional: float, stop_pct: float) -> bool:
    """True if a single position has lost more than stop_pct of its OWN entry value.

    This is the per-name stop that caps short-squeeze / blow-up risk (e.g. the ACE short that
    squeezed +89% and lost ~90% of its notional). Measuring against the position's own value —
    not total equity — is what makes it fire on a single name before it wrecks the book.
    A short that squeezes +30% loses 30% of its notional -> breached at stop_pct=30.
    """
    if entry_notional <= 0 or stop_pct <= 0:
        return False
    return pnl < -(stop_pct / 100.0) * entry_notional


def filter_by_funding(funding: dict[str, float], abs_max: float) -> list[str]:
    """Symbols whose |funding rate| is within the allowed band (drops extreme-funding names)."""
    if abs_max <= 0:
        return list(funding.keys())
    return [s for s, f in funding.items() if abs(f) <= abs_max]


@dataclass
class RiskReport:
    equity: float
    gross_notional: float
    net_notional: float
    gross_leverage: float
    net_ratio: float
    leverage_scale: float
    net_ok: bool
    drawdown_pct: float
    drawdown_breached: bool
