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
    # -- execution learning (optional; paper trains on the REAL book, never on its own fills) --
    exec_policy: object = None
    store: object = None
    fx: object = None          # exchange handle, used only to read l2Book depth

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
                # realism: only maker_fill_ratio of POST_ONLY orders rest-and-fill as maker; the rest
                # chase across the spread as taker (higher fee + slippage). Deterministic cost blend.
                mr = min(1.0, max(0.0, getattr(exec_cfg, "maker_fill_ratio", 1.0)))
                mk = spec.open_maker_fee if o.open == "OPEN" else spec.close_maker_fee
                if mr >= 1.0:
                    fill_price = spec.round_price(o.ref_price)   # all maker, resting at touch
                    fee_rate = mk
                else:
                    touch = spec.round_price(o.ref_price)
                    taker_px = marketable_price(o.side, o.ref_price, exec_cfg.slippage_bps, spec)
                    fill_price = spec.round_price(mr * touch + (1.0 - mr) * taker_px)
                    tk = spec.open_taker_fee if o.open == "OPEN" else spec.close_taker_fee
                    fee_rate = mr * mk + (1.0 - mr) * tk
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
            self._learn_from_book(o, exec_cfg)
        return fills

    def _learn_from_book(self, o: Order, exec_cfg) -> None:
        """Train the execution policy on the REAL order book, not on our simulated fill.

        The fill above is synthetic — it is `maker_fill_ratio` blended between touch and taker, a
        config constant. Learning from it would teach the policy our own assumption back. The book
        read here is the same l2Book a live trader sees, so the spread, the depth and the true cost
        of crossing OUR size are all real. That is what gets recorded.

        Never raises: paper trading must not be able to fail because a book read did.
        """
        if self.exec_policy is None or self.fx is None:
            return
        try:
            from .book_probe import probe
            bs = probe(self.fx, o.symbol, o.side, o.notional)
            if bs is None:
                return
            ctx = self.exec_policy.context(spread_bps=bs.spread_bps, daily_vol=0.04,
                                           urgent=(o.open == "CLOSE"))
            arm = self.exec_policy.choose(ctx)
            # CROSS is fully observable right now: walking the real book IS its cost.
            # POST is not — whether a resting order fills depends on subsequent price action — so
            # it is logged unresolved and settled next cycle rather than guessed at here.
            if arm == "CROSS":
                self.exec_policy.record(ctx, arm, bs.cross_bps)
            if self.store is not None:
                half = bs.half_spread_bps / 1e4
                touch = bs.mid * (1 - half) if o.side.upper() == "BUY" else bs.mid * (1 + half)
                rid = self.store.record_exec(
                    self.mode, symbol=o.symbol, side=o.side, context=ctx, arm=arm, shadow=arm,
                    arrival_mid=bs.mid, notional=float(o.notional), ref_price=touch)
                if arm == "CROSS":
                    self.store.resolve_exec(rid, fill_price=bs.mid * (1 + bs.cross_bps / 1e4),
                                            cost_bps=bs.cross_bps, filled=True)
        except Exception as e:                                # noqa: BLE001
            import logging
            logging.getLogger("sentinel").debug("paper exec learning skipped: %s", e)

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
