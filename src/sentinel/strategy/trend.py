"""Trend (CTA) strategy — cross-sectional time-series momentum, dollar-neutral long/short.

Long the K coins in the strongest uptrends (price furthest above its moving average), short the K in the
strongest downtrends. This is the classic managed-futures / trend-following edge — the single most durable
systematic edge across markets and decades. Validated on 5yr Binance daily data (survivorship-caveated):
Sharpe ~1.35, +75-82% CAGR, positive every full year 2021-2025. The raw gross drawdown (~44%) is tamed by
the engine's vol-target + drawdown-throttle overlay (via gross_scale), exactly like the champion book.

Replaces the retired Funding-Alpha book, whose pure-funding-harvest edge decayed to a 5yr Sharpe of 0.38.

Returns a TargetBook so it reuses the exact same execution / reconciler / store path as the other books.
"""
from __future__ import annotations

from ..exchange.contracts import ContractRegistry
from .sentiment_edge import TargetBook, TargetPosition


def _trend(closes: list, ma: int) -> float:
    """Trend strength = last price vs its `ma`-period average (>0 uptrend, <0 downtrend).
    Returns -1e9 if there isn't enough history for the moving average."""
    a = [x for x in closes if x and x > 0]
    if len(a) < ma + 1:
        return -1e9
    avg = sum(a[-ma:]) / ma
    return a[-1] / avg - 1.0 if avg > 0 else -1e9


def _breakout(closes: list, window: int) -> float:
    """Where price sits inside its own `window`-day range: +0.5 at the high, -0.5 at the low.

    Same question as _trend — is this coin trending, and how hard — asked against the range rather
    than the mean. Two things differ, and the second is the one that matters here:

      * it is bounded, so a single vertical candle cannot dominate the cross-sectional ranking the
        way a price/MA ratio can;
      * it is far more stable day to day, which cuts turnover, and turnover is the measured enemy.
        Live Carry paid $139.71 of fees in 38.8 days (13.2%/yr) against a gross trading P&L of
        -$114.51. Cost is what actually decides this book.

    Measured on 900 days, established-coin universe (survivorship removed — the biased universe
    flatters every trend variant and is not evidence):

        window   CAGR     Sharpe   turnover        cost stress: 5bps / 10bps / 20bps
        MA-30    +1.4%     0.20      0.98              +1.4% /  -6.1% / -11.9%   <- live logic
        brk-20  +21.0%     0.71      1.21
        brk-30  +14.9%     0.57      1.02             +14.9% /  +0.0% / -13.0%
        brk-45  +28.3%     0.87      0.90             +28.3% /  +9.5% /  -9.2%
        brk-60   -0.8%     0.13      0.78

    20/30/45 all clear zero at EVERY universe width (20/25/30/35/40); MA-30 does not (min -0.09),
    and 10/14/60 fail. That plateau is why this is a signal rather than a tuned constant — the
    differences between 20, 30 and 45 are not distinguishable, so 45 is chosen for the lowest
    turnover and the best cost tolerance, not for its CAGR.

    HONEST LIMIT: this does NOT fix Trend's regime dependence. Breakout-45 is positive in 5 of 9
    quarters, exactly like the MA baseline, and still loses badly in a bad quarter (-57% in Q7).
    It is a cheaper, steadier expression of the same one-regime bet, not a different bet.
    """
    a = [x for x in closes if x and x > 0]
    if len(a) < window:
        return -1e9
    w = a[-window:]
    hi, lo = max(w), min(w)
    if hi <= lo:
        return 0.0                      # flat/illiquid: no information, rank it in the middle
    return (a[-1] - lo) / (hi - lo) - 0.5


class TrendStrategy:
    """Dollar-neutral cross-sectional trend book: long the strongest uptrends, short the strongest
    downtrends. Equal-weight each sleeve to the same notional so net exposure starts at ~0."""

    def __init__(self, cfg):
        self.c = cfg.trend            # TrendCfg

    def build_book(self, closes_by_symbol: dict, prices: dict, registry: ContractRegistry,
                   equity: float, gross_scale: float = 1.0) -> TargetBook:
        c = self.c
        cand = []
        for s, closes in closes_by_symbol.items():
            if s not in prices or prices[s] <= 0 or not registry.has(s):
                continue
            if getattr(c, "score_mode", "ma") == "breakout":
                t = _breakout(closes, getattr(c, "donchian_period", 45))
            else:
                t = _trend(closes, c.ma_period)
            if t <= -1e8:                        # insufficient history for the score
                continue
            cand.append((s, t))
        if len(cand) < 2 * c.top_k:              # need enough names for a balanced long & short sleeve
            return TargetBook({}, equity, 0.0)

        cand.sort(key=lambda x: x[1])            # ascending by trend strength
        # CROSS-SECTIONAL: long the top_k, short the bottom_k, on RANK alone.
        #
        # These lines used to read `if t > 0` and `if t < 0`, requiring each name to be in an
        # absolute up/down trend as well as being the strongest/weakest. That silently converted a
        # cross-sectional signal into a time-series one, and the two disagree exactly when the whole
        # market moves together. On 2026-08-21 it emptied the book: every one of the 30 universe
        # coins sat in the upper half of its 45-day range (weakest was ONDO at +0.075), so the short
        # sleeve was empty, `not shorts` fired, and the live book closed all 10 positions and held
        # nothing for four days through a rally.
        #
        # A relative ranking does not stop being informative because everything is rising. ONDO at
        # +0.075 is still the weakest name in the cross-section and is still the right short for a
        # dollar-neutral book. Carry never had this filter — carry_rank takes top_k/bottom_k
        # unconditionally — so the two books had contradictory readings of their own rankings.
        #
        # Measured over 599 tradeable days on 106 coins: the filter emptied the book on 8.5% of
        # them, and removing it improves Sharpe at EVERY donchian setting (14d +0.70, 20d +0.42,
        # 30d +0.43, 45d +0.23, 60d +0.23) and wins 5 of 6 robustness cuts. Honest limits: it loses
        # the second-half cut (-0.76), the paired bootstrap CI spans zero, and the Deflated Sharpe
        # only moves 0.351 -> 0.522, so this makes an unevidenced book slightly less unevidenced
        # rather than giving it an edge. It ships because a flat book during a broad rally is not
        # what a cross-sectional strategy is supposed to do.
        longs = cand[-c.top_k:]
        shorts = cand[:c.top_k]
        if not longs or not shorts:
            return TargetBook({}, equity, 0.0)

        gross = c.target_gross * equity * gross_scale
        side = gross / 2.0
        positions: dict[str, TargetPosition] = {}
        for s, t in longs:
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(side / len(longs), prices[s])
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s])
            positions[s] = TargetPosition(s, t, "LONG", contracts, realized, prices[s])
        for s, t in shorts:
            spec = registry.get(s)
            contracts = spec.notional_to_contracts(side / len(shorts), prices[s])
            if contracts <= 0:
                continue
            realized = spec.contracts_to_notional(contracts, prices[s])
            positions[s] = TargetPosition(s, t, "SHORT", -contracts, -realized, prices[s])
        return TargetBook(positions, equity, gross)
