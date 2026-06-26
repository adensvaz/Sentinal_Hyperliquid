"""Paper broker: simulates fills at the marketable-limit price and tracks virtual positions, fees,
realized + unrealized PnL, and equity. Same code path as live (reconciler -> orders -> execute), so the
paper track record is representative.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..exchange.contracts import ContractRegistry
from .broker import Broker, Fill, Order, marketable_price


@dataclass
class PaperPosition:
    contracts: int = 0       # signed
    avg_price: float = 0.0
    multiplier: float = 1.0
    opened_ts: float = 0.0   # epoch seconds when the position was first opened (for trade duration)


@dataclass
class PaperBroker(Broker):
    starting_capital: float
    mode: str = "paper"
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_pnl: float = 0.0       # cumulative funding collected (+) / paid (-)
    last_funding_ts: float = 0.0   # last time funding was accrued
    _positions: dict[str, PaperPosition] = field(default_factory=dict)
    _marks: dict[str, float] = field(default_factory=dict)
    closed_trades: list = field(default_factory=list)   # round-trips drained to the store each cycle

    # -- broker interface ----------------------------------------------------
    def positions(self) -> dict[str, int]:
        return {s: p.contracts for s, p in self._positions.items() if p.contracts != 0}

    def set_marks(self, prices: dict[str, float]) -> None:
        self._marks.update({s: float(p) for s, p in prices.items() if p})

    def unrealized_pnl(self) -> float:
        total = 0.0
        for sym, pos in self._positions.items():
            mark = self._marks.get(sym, pos.avg_price)
            total += (mark - pos.avg_price) * pos.contracts * pos.multiplier
        return total

    def equity(self) -> float:
        return self.starting_capital + self.realized_pnl + self.funding_pnl + self.unrealized_pnl()

    def accrue_funding(self, funding_rates: dict, marks: dict, interval_hours: float = 8.0,
                       now: float = None) -> None:
        """Accrue funding P&L on open positions for the time elapsed since the last call.
        Carry to us per interval = -side * rate * notional (longs pay when rate>0; shorts collect)."""
        now = now if now is not None else time.time()
        if self.last_funding_ts and now > self.last_funding_ts:
            hours = (now - self.last_funding_ts) / 3600.0
            for sym, pos in self._positions.items():
                if pos.contracts == 0:
                    continue
                rate = funding_rates.get(sym)
                mark = marks.get(sym, pos.avg_price)
                if rate is None or mark <= 0:
                    continue
                notional = abs(pos.contracts) * mark * pos.multiplier
                sign = 1 if pos.contracts > 0 else -1
                self.funding_pnl += -sign * rate * notional * (hours / interval_hours)
        self.last_funding_ts = now

    def execute(self, orders: list[Order], registry: ContractRegistry, exec_cfg) -> list[Fill]:
        fills: list[Fill] = []
        maker = getattr(exec_cfg, "maker", False)
        for o in orders:
            spec = registry.get(o.symbol)
            if maker:
                fill_price = spec.round_price(o.ref_price)   # resting maker fill, no spread-crossing
                fee_rate = spec.open_maker_fee if o.open == "OPEN" else spec.close_maker_fee
            elif exec_cfg.order_type == "limit":
                fill_price = marketable_price(o.side, o.ref_price, exec_cfg.slippage_bps, spec)
                fee_rate = spec.open_taker_fee if o.open == "OPEN" else spec.close_taker_fee
            else:
                fill_price = spec.round_price(o.ref_price)
                fee_rate = spec.open_taker_fee if o.open == "OPEN" else spec.close_taker_fee
            fee = abs(o.volume) * fill_price * spec.multiplier * fee_rate
            self._apply(o, fill_price, fee, spec.multiplier)
            self.fees_paid += fee
            fills.append(Fill(o.symbol, o.side, o.open, o.volume, fill_price, fee,
                              order_id=f"paper-{len(fills)}"))
        return fills

    # -- position bookkeeping -------------------------------------------------
    def _apply(self, o: Order, price: float, fee: float, multiplier: float) -> None:
        pos = self._positions.setdefault(o.symbol, PaperPosition(multiplier=multiplier))
        pos.multiplier = multiplier
        delta = o.signed_delta            # +vol for BUY, -vol for SELL
        cur = pos.contracts
        self.realized_pnl -= fee          # fees reduce realized PnL

        if cur == 0 or (cur > 0) == (delta > 0):
            # opening or adding in the same direction -> weighted-average entry
            if cur == 0:
                pos.opened_ts = time.time()
            new_contracts = cur + delta
            if new_contracts != 0:
                pos.avg_price = (pos.avg_price * abs(cur) + price * abs(delta)) / abs(new_contracts)
            pos.contracts = new_contracts
            return

        # reducing / closing / flipping -> realize a closed round-trip
        closed = min(abs(cur), abs(delta))
        direction = 1 if cur > 0 else -1
        pnl = (price - pos.avg_price) * closed * direction * multiplier
        self.realized_pnl += pnl
        self.closed_trades.append({
            "symbol": o.symbol, "side": "LONG" if cur > 0 else "SHORT",
            "entry": pos.avg_price, "exit": price, "contracts": int(closed),
            "pnl": pnl, "opened_ts": pos.opened_ts, "closed_ts": time.time(),
        })
        remaining = abs(cur) - abs(delta)
        if remaining > 0:
            pos.contracts = direction * remaining   # same side, smaller
        elif remaining == 0:
            pos.contracts = 0
            pos.avg_price = 0.0
        else:
            # flipped to the other side; leftover opens at fill price (a new position)
            pos.contracts = -direction * (abs(delta) - abs(cur))
            pos.avg_price = price
            pos.opened_ts = time.time()
