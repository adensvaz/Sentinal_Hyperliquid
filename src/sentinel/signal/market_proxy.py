"""Market-derived proxy for a 'sentiment' score.

Per symbol we compute three raw features from KoinBay's own futures data:
  - risk-adjusted momentum: blended multi-horizon return / realized vol
  - funding/crowding:        funding_sign * currentFundRate (default -1 = fade crowded longs)
  - volume surge:            recent volume vs the window's baseline
Each is z-scored ACROSS the universe, combined with configured weights, and the composite is z-scored
again to yield the final continuous score (higher = go long, lower = go short).

This is an honest momentum/flow/crowding proxy, not true social sentiment — but it plugs into the same
SignalProvider seam, so a real feed can replace it later.
"""
from __future__ import annotations

from ..util.mathx import EPS, realized_vol, value_at_or_before, zscore
from .base import SignalProvider, SymbolData


class MarketProxySignal(SignalProvider):
    def __init__(
        self,
        horizons_hours: list[float],
        weights: dict[str, float],
        funding_sign: int = -1,
        base_interval: str = "4h",
        history_bars: int = 120,
        volume_recent_bars: int = 6,
        reversal_hours: float = 24.0,
    ):
        self.horizons_hours = sorted(horizons_hours)
        self.w_mom = float(weights.get("momentum", 0.5))
        self.w_fund = float(weights.get("funding", 0.3))
        self.w_fundvel = float(weights.get("funding_vel", 0.0))
        self.w_vol = float(weights.get("volume", 0.2))
        self.w_whale = float(weights.get("whale", 0.0))
        self.w_rev = float(weights.get("reversal", 0.0))
        self.funding_sign = funding_sign
        self.base_interval = base_interval
        self.history_bars = history_bars
        self.volume_recent_bars = volume_recent_bars
        self.reversal_hours = reversal_hours

    # -- per-symbol raw features ---------------------------------------------
    def _risk_adjusted_momentum(self, sd: SymbolData) -> float:
        if sd.n_bars < 3:
            return 0.0
        last_close = sd.closes[-1]
        last_idx = sd.idx_ms[-1]
        rets: list[float] = []
        for h in self.horizons_hours:
            target = last_idx - int(h * 3_600_000)
            past = value_at_or_before(sd.idx_ms, sd.closes, target)
            if past and past > 0 and past != last_close:
                rets.append(last_close / past - 1.0)
        if not rets:
            return 0.0
        mom = sum(rets) / len(rets)
        vol = realized_vol(sd.closes)
        return mom / (vol + EPS)

    def _reversal(self, sd: SymbolData) -> float:
        """Short-horizon reversal: the NEGATIVE of the recent return (fade recent movers)."""
        if sd.n_bars < 2:
            return 0.0
        last, last_idx = sd.closes[-1], sd.idx_ms[-1]
        past = value_at_or_before(sd.idx_ms, sd.closes, last_idx - int(self.reversal_hours * 3_600_000))
        if not past or past <= 0 or past == last:
            return 0.0
        return -(last / past - 1.0)

    def _volume_surge(self, sd: SymbolData) -> float:
        v = sd.volumes
        if len(v) < self.volume_recent_bars + 1:
            return 0.0
        recent = sum(v[-self.volume_recent_bars:]) / self.volume_recent_bars
        base = sum(v) / len(v)
        if base <= EPS:
            return 0.0
        return recent / base - 1.0

    # -- cross-sectional combination -----------------------------------------
    def scores(self, data: dict[str, SymbolData],
               whale_bias: dict[str, float] | None = None) -> dict[str, float]:
        """Continuous score per symbol. `whale_bias` maps symbol -> smart-money consensus
        (~[-1, 1]); symbols absent from it contribute 0 to the whale component."""
        whale_bias = whale_bias or {}
        syms = [s for s, sd in data.items() if sd.n_bars >= 3]
        if not syms:
            return {}
        mom = [self._risk_adjusted_momentum(data[s]) for s in syms]
        fund = [self.funding_sign * data[s].funding for s in syms]
        # funding velocity: forward minus current funding — positioning ACCELERATION, not level.
        # Same contrarian sign as the level (funding rising into crowding = bearish). Orthogonal-ish
        # to the level, so it adds genuine breadth. (Inert in the kline backtest: no historical funding.)
        fundvel = [self.funding_sign * (data[s].next_funding - data[s].funding) for s in syms]
        vsurge = [self._volume_surge(data[s]) for s in syms]
        whale = [float(whale_bias.get(s, 0.0)) for s in syms]
        rev = [self._reversal(data[s]) for s in syms]

        z_mom = zscore(mom)
        z_fund = zscore(fund)
        z_fundvel = zscore(fundvel) if self.w_fundvel else [0.0] * len(syms)
        z_vsurge = zscore(vsurge)
        z_whale = zscore(whale) if self.w_whale else [0.0] * len(syms)
        z_rev = zscore(rev) if self.w_rev else [0.0] * len(syms)

        composite = [
            self.w_mom * z_mom[i] + self.w_fund * z_fund[i] + self.w_fundvel * z_fundvel[i]
            + self.w_vol * z_vsurge[i] + self.w_whale * z_whale[i] + self.w_rev * z_rev[i]
            for i in range(len(syms))
        ]
        final = zscore(composite)
        return {syms[i]: final[i] for i in range(len(syms))}
