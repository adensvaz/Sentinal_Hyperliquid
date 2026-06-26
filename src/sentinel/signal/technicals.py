"""Native technical-analysis consensus — a TradingView-style Buy/Neutral/Sell gauge computed from the
klines we already fetch (no external API, no scraping, no key).

TradingView's rating is just an aggregate of standard indicators (moving averages + oscillators). We
reproduce the spirit of it from close prices: each indicator votes BUY(+1)/SELL(-1)/NEUTRAL(0); the mean
vote is a consensus in [-1, +1]. Used as a pre-trade VETO (don't long a "Sell", don't short a "Buy") —
a timing/quality filter on top of the cross-sectional signal, not a signal itself.
"""
from __future__ import annotations

import numpy as np


def _sma(c: np.ndarray, n: int):
    return float(c[-n:].mean()) if len(c) >= n else None


def _ema_series(c: np.ndarray, n: int) -> np.ndarray:
    k = 2.0 / (n + 1)
    out = np.empty_like(c)
    out[0] = c[0]
    for i in range(1, len(c)):
        out[i] = c[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(c: np.ndarray, n: int = 14):
    if len(c) < n + 1:
        return None
    d = np.diff(c[-(n + 1):])
    up = d[d > 0].sum() / n
    dn = -d[d < 0].sum() / n
    if dn == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / dn)


def ta_consensus(closes: list) -> float:
    """Aggregate indicator vote in [-1, +1] from a close series (higher = bullish)."""
    c = np.asarray([x for x in closes if x and x > 0], dtype=float)
    if len(c) < 30:
        return 0.0   # not enough history -> neutral (no veto)
    price = c[-1]
    votes = []
    # Moving-average votes: price above MA = bullish
    for n in (10, 20, 30):
        s = _sma(c, n)
        if s:
            votes.append(1 if price > s else -1)
        if len(c) >= n:
            e = _ema_series(c, n)[-1]
            votes.append(1 if price > e else -1)
    # MACD(12,26,9): line above signal = bullish
    if len(c) >= 35:
        macd = _ema_series(c, 12) - _ema_series(c, 26)
        sig = _ema_series(macd, 9)
        votes.append(1 if macd[-1] > sig[-1] else -1)
    # RSI(14): oversold = bullish, overbought = bearish
    r = _rsi(c)
    if r is not None:
        votes.append(1 if r < 30 else (-1 if r > 70 else 0))
    # Momentum(10)
    if len(c) >= 11:
        votes.append(1 if c[-1] > c[-11] else -1)
    return sum(votes) / len(votes) if votes else 0.0


def ta_label(score: float) -> str:
    if score >= 0.5:
        return "Strong Buy"
    if score >= 0.1:
        return "Buy"
    if score <= -0.5:
        return "Strong Sell"
    if score <= -0.1:
        return "Sell"
    return "Neutral"


def passes_veto(ta_score: float, side: str, threshold: float) -> bool:
    """True if technicals don't contradict the trade: no LONG when clearly bearish, no SHORT when
    clearly bullish. threshold<=0 disables the veto."""
    if threshold <= 0:
        return True
    if side == "LONG":
        return ta_score >= -threshold
    return ta_score <= threshold
