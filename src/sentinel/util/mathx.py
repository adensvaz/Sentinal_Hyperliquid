"""Small numeric helpers used by the signal and sizing layers."""
from __future__ import annotations

import bisect

import numpy as np

EPS = 1e-12


def zscore(values: list[float]) -> list[float]:
    """Cross-sectional z-score. Returns zeros if there's no spread or <2 points."""
    a = np.asarray(values, dtype=float)
    if a.size < 2:
        return [0.0] * a.size
    sd = a.std()
    if sd < EPS:
        return [0.0] * a.size
    return ((a - a.mean()) / sd).tolist()


def log_returns(closes: list[float]) -> list[float]:
    a = np.asarray(closes, dtype=float)
    if a.size < 2:
        return []
    a = np.clip(a, EPS, None)
    return np.diff(np.log(a)).tolist()


def realized_vol(closes: list[float]) -> float:
    lr = log_returns(closes)
    if len(lr) < 2:
        return 0.0
    return float(np.std(lr))


def value_at_or_before(idx_ms: list[int], values: list[float], target_ms: int):
    """Given an ascending-by-time series, return the value whose timestamp is the
    latest one <= target_ms (or None if target precedes the series)."""
    if not idx_ms:
        return None
    pos = bisect.bisect_right(idx_ms, target_ms) - 1
    if pos < 0:
        return None
    return values[pos]


def parse_duration_to_hours(s: str) -> float:
    """'4h'->4, '24h'->24, '7d'->168, '30min'->0.5, '1day'->24, '1week'->168, '1month'->720."""
    s = s.strip().lower()
    units = [("month", 720.0), ("week", 168.0), ("min", 1 / 60.0),
             ("day", 24.0), ("h", 1.0), ("d", 24.0), ("w", 168.0), ("m", 1 / 60.0)]
    for suffix, hours in units:
        if s.endswith(suffix):
            num = s[: -len(suffix)] or "1"
            try:
                return float(num) * hours
            except ValueError:
                break
    raise ValueError(f"cannot parse duration: {s!r}")
