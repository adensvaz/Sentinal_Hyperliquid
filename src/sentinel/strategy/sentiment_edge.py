"""Sentiment Edge strategy: continuous scores -> dollar-neutral long/short target book.

Long the top-scored names, short the bottom-scored, weight within each sleeve by |score| (capped),
and size both sleeves to the same notional so net exposure starts at ~0.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import PortfolioCfg
from ..exchange.contracts import ContractRegistry
from .sizing import capped_weights, sleeve_notionals, vol_adjust


@dataclass
class TargetPosition:
    symbol: str
    score: float
    side: str            # "LONG" | "SHORT"
    target_contracts: int  # signed (+long / -short)
    target_notional: float  # signed, realized after rounding to contracts
    price: float


@dataclass
class TargetBook:
    positions: dict[str, TargetPosition]
    equity: float
    target_gross: float  # intended gross notional before contract rounding

    def signed_contracts(self) -> dict[str, int]:
        return {s: p.target_contracts for s, p in self.positions.items()}

    @property
    def realized_gross(self) -> float:
        return sum(abs(p.target_notional) for p in self.positions.values())

    @property
    def realized_net(self) -> float:
        return sum(p.target_notional for p in self.positions.values())

    @property
    def long_notional(self) -> float:
        return sum(p.target_notional for p in self.positions.values() if p.target_contracts > 0)

    @property
    def short_notional(self) -> float:
        return sum(-p.target_notional for p in self.positions.values() if p.target_contracts < 0)


class SentimentEdgeStrategy:
    def __init__(self, portfolio: PortfolioCfg):
        self.p = portfolio

    @staticmethod
    def _pick(seq, n, side, ta, ta_veto, used):
        """Pick up to n names from seq (sorted appropriately) that pass the TA veto and aren't used."""
        from ..signal.technicals import passes_veto
        out = []
        for sym, sc in seq:
            if sym in used:
                continue
            if ta is not None and not passes_veto(ta.get(sym, 0.0), side, ta_veto):
                continue
            out.append((sym, sc))
            used.add(sym)
            if len(out) >= n:
                break
        return out

    def build_book(
        self,
        scores: dict[str, float],
        prices: dict[str, float],
        registry: ContractRegistry,
        equity: float,
        gross_scale: float = 1.0,
        ta: dict = None,
        ta_veto: float = 0.0,
        vol_by_symbol: dict = None,
    ) -> TargetBook:
        gross = self.p.target_gross_leverage * equity * gross_scale
        scored = [
            (s, sc) for s, sc in scores.items()
            if s in prices and prices[s] > 0 and registry.has(s)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)

        n_long, n_short = self.p.longs, self.p.shorts
        # shrink sleeves so longs/shorts stay disjoint if the universe is small
        if len(scored) < n_long + n_short:
            half = len(scored) // 2
            n_long, n_short = min(n_long, half), min(n_short, half)
        if n_long < 1 or n_short < 1:
            return TargetBook({}, equity, gross)

        used: set = set()
        longs = self._pick(scored, n_long, "LONG", ta, ta_veto, used)            # top-down
        shorts = self._pick(list(reversed(scored)), n_short, "SHORT", ta, ta_veto, used)  # bottom-up

        if self.p.weighting == "equal":
            raw_long = [1.0] * len(longs)
            raw_short = [1.0] * len(shorts)
        else:  # "score": weight by relative strength^conviction within the sleeve
            k = self.p.conviction
            raw_long = [max(sc, 0.0) ** k for _, sc in longs]
            raw_short = [max(-sc, 0.0) ** k for _, sc in shorts]

        # risk parity: damp each name by its volatility (equal-risk, not equal-dollar)
        if self.p.risk_parity and vol_by_symbol:
            raw_long = vol_adjust(raw_long, [vol_by_symbol.get(s, 0.0) for s, _ in longs])
            raw_short = vol_adjust(raw_short, [vol_by_symbol.get(s, 0.0) for s, _ in shorts])

        w_long = capped_weights(raw_long, self.p.max_name_weight)
        w_short = capped_weights(raw_short, self.p.max_name_weight)
        sleeve = gross / 2.0

        positions: dict[str, TargetPosition] = {}
        for (sym, sc), notional in zip(longs, sleeve_notionals(w_long, sleeve, +1)):
            self._add(positions, registry, sym, sc, "LONG", notional, prices[sym])
        for (sym, sc), notional in zip(shorts, sleeve_notionals(w_short, sleeve, -1)):
            self._add(positions, registry, sym, sc, "SHORT", notional, prices[sym])
        return TargetBook(positions, equity, gross)

    @staticmethod
    def _add(positions, registry, sym, score, side, notional, price):
        spec = registry.get(sym)
        contracts = spec.notional_to_contracts(notional, price)
        if contracts == 0:
            return  # below minimum order volume
        realized = spec.contracts_to_notional(contracts, price)
        signed_notional = realized if contracts > 0 else -realized
        positions[sym] = TargetPosition(sym, score, side, contracts, signed_notional, price)
