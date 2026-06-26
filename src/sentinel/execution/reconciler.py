"""Turn (target book vs current positions) into the minimal set of orders.

Rules:
- grow a position (same side, larger magnitude)         -> OPEN the delta
- shrink a position (same side, smaller magnitude)       -> CLOSE the delta
- flip sign (long<->short)                               -> CLOSE all, then OPEN the new target
- open a brand-new position                              -> OPEN the target
- drop a position entirely (target 0)                    -> CLOSE all
`min_trade_notional` suppresses dust OPENs and tiny same-side adjustments (fee/churn guard); full
closes and flip-closes always execute so the book exits cleanly.
"""
from __future__ import annotations

from ..exchange.contracts import ContractRegistry
from .broker import Order


def _emit(out: list[Order], sym: str, side: str, open_: str, volume: int, price: float, notional: float):
    if volume > 0:
        out.append(Order(sym, side, open_, int(volume), price, notional))


def orders_for_symbol(sym, cur, tgt, price, spec, min_notional, min_frac=0.0) -> list[Order]:
    out: list[Order] = []
    if cur == tgt or price <= 0:
        return out

    def notional(vol: int) -> float:
        return spec.contracts_to_notional(vol, price)

    same_sign = (cur > 0 and tgt > 0) or (cur < 0 and tgt < 0)

    # sign flip: close current fully, then open new target
    if cur != 0 and tgt != 0 and not same_sign:
        _emit(out, sym, "SELL" if cur > 0 else "BUY", "CLOSE", abs(cur), price, notional(abs(cur)))
        if notional(abs(tgt)) >= min_notional:
            _emit(out, sym, "BUY" if tgt > 0 else "SELL", "OPEN", abs(tgt), price, notional(abs(tgt)))
        return out

    if cur == 0:  # brand-new position
        if notional(abs(tgt)) >= min_notional:
            _emit(out, sym, "BUY" if tgt > 0 else "SELL", "OPEN", abs(tgt), price, notional(abs(tgt)))
        return out

    if tgt == 0:  # full close
        _emit(out, sym, "SELL" if cur > 0 else "BUY", "CLOSE", abs(cur), price, notional(abs(cur)))
        return out

    # same sign, adjust magnitude
    delta = abs(tgt - cur)
    if min_frac > 0 and abs(cur) > 0 and delta < min_frac * abs(cur):
        return out  # within the no-trade band -> leave the position alone (anti-churn)
    if abs(tgt) > abs(cur):  # grow
        if notional(delta) >= min_notional:
            _emit(out, sym, "BUY" if tgt > 0 else "SELL", "OPEN", delta, price, notional(delta))
    else:  # shrink
        if notional(delta) >= min_notional:
            _emit(out, sym, "SELL" if cur > 0 else "BUY", "CLOSE", delta, price, notional(delta))
    return out


def reconcile(
    target: dict[str, int],
    current: dict[str, int],
    prices: dict[str, float],
    registry: ContractRegistry,
    min_trade_notional: float,
    min_rebalance_frac: float = 0.0,
) -> list[Order]:
    orders: list[Order] = []
    for sym in sorted(set(target) | set(current)):
        if not registry.has(sym):
            continue
        cur = int(current.get(sym, 0))
        tgt = int(target.get(sym, 0))
        price = float(prices.get(sym, 0.0))
        orders.extend(orders_for_symbol(sym, cur, tgt, price, registry.get(sym),
                                        min_trade_notional, min_rebalance_frac))
    return orders
