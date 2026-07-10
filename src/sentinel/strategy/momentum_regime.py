"""Champion strategy — directional trend/momentum with a market-regime brake.

Exhaustively validated on 5.5yr survivorship-free Binance data (Sharpe ~1.1-1.2 vs buy-hold 0.35,
walk-forward robust across both halves). Unlike the market-neutral book, this KEEPS crypto's beta —
which the long backtest proved is where the durable edge lives — but gates it with a regime filter:

  1. REGIME: only hold risk if BTC is above its `regime_ma`-day average (else go to cash).
  2. SELECT: rank coins by `lookback`-day momentum; keep the top-K (optionally also require each to be
     above its own `trend_ma`-day average — dual confirmation).
  2b. BREADTH (optional): scale gross by the fraction of the universe in an uptrend — de-risks thin,
     narrow rallies. A parallel market-health check; cuts drawdowns hard with ~no robustness loss.
  3. SIZE: long-only, equal-weight OR momentum-weighted (capped per-name for copy-trade safety).
     gross = target_gross * gross_scale * breadth_scale (vol-target/DD-throttle feed in via gross_scale).
     No shorts, no beta-neutralization — the market exposure is the point.

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


def breadth_fraction(eligible_closes: list, ma_bars: int) -> float:
    """Fraction of the tradable universe currently trading above its `ma_bars` average.
    A breadth/market-health gauge: broad uptrend -> near 1.0; only a few names holding up -> near 0."""
    if not eligible_closes:
        return 0.0
    above = sum(1 for cl in eligible_closes if _above_ma(cl, ma_bars))
    return above / len(eligible_closes)


def breadth_scale(fraction: float, lo: float, hi: float) -> float:
    """Linear gross scaler: 0 at/below `lo` breadth, 1.0 at/above `hi`, ramped in between."""
    if hi <= lo:
        return 1.0
    return min(1.0, max(0.0, (fraction - lo) / (hi - lo)))


def capped_weights(strength: dict, cap: float) -> dict:
    """Momentum-proportional weights (sum to 1) with no single name above `cap`.
    Iteratively clamps over-cap names and redistributes the excess to the rest (water-filling)."""
    pos = {s: max(v, 1e-9) for s, v in strength.items()}
    tot = sum(pos.values()) or 1.0
    w = {s: v / tot for s, v in pos.items()}
    if not cap or cap <= 0 or cap >= 1 or len(w) * cap < 1.0:
        return w  # cap can't be satisfied (or disabled) -> fall back to proportional
    for _ in range(8):
        over = [s for s, x in w.items() if x > cap + 1e-12]
        if not over:
            break
        excess = sum(w[s] - cap for s in over)
        for s in over:
            w[s] = cap
        under = [s for s in w if w[s] < cap - 1e-12]
        base = sum(w[s] for s in under)
        if base <= 0:
            break
        for s in under:
            w[s] += excess * w[s] / base
    return w


class MomentumRegimeStrategy:
    """Long-only top-K momentum with a BTC-regime brake (the backtest champion)."""

    def __init__(self, cfg):
        self.c = cfg.champion           # ChampionCfg
        # venue-aware BTC reference for the regime brake: Hyperliquid names it "BTC", KoinBay "E-BTC-USDT".
        # (Hardcoding the KoinBay symbol left Champion's regime gate dead on HL — always cash.)
        self.ref = "BTC" if cfg.exchange.venue == "hyperliquid" else "E-BTC-USDT"

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
        eligible_closes = []
        for s, closes in closes_by_symbol.items():
            if s not in prices or prices[s] <= 0 or not registry.has(s):
                continue
            eligible_closes.append(closes)   # tradable universe (for the breadth gauge)
            if c.dual_confirm and not _above_ma(closes, c.trend_ma):
                continue
            m = _momentum(closes, c.lookback)
            if m > -1e8:
                cand.append((s, m))
        # 2b. breadth check — de-risk when only a narrow slice of the market is trending up
        if getattr(c, "breadth_scale", False):
            frac = breadth_fraction(eligible_closes, c.breadth_ma)
            gross *= breadth_scale(frac, c.breadth_lo, c.breadth_hi)
            if gross <= 0:
                return TargetBook({}, equity, 0.0)
        cand.sort(key=lambda x: x[1], reverse=True)
        picks = [(s, m) for s, m in cand[: c.top_k] if m > 0]   # only positive momentum
        if not picks:
            return TargetBook({}, equity, 0.0)
        # 3. size long-only: equal-weight, or momentum-weighted (capped per-name) if enabled
        if getattr(c, "mom_weighted", False):
            weights = capped_weights({s: m for s, m in picks}, c.max_name_weight)
        else:
            weights = {s: 1.0 / len(picks) for s, m in picks}
        positions: dict[str, TargetPosition] = {}
        for s, m in picks:
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(gross * weights[s], prices[s])
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s])
            positions[s] = TargetPosition(s, m, "LONG", contracts, realized, prices[s])
        return TargetBook(positions, equity, gross)
