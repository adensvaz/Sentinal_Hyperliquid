"""Live broker: executes the target book on real KoinBay futures.

Reads equity + positions from GET /fapi/v1/account, sets per-contract margin/position-mode/leverage
once via the edit_* endpoints, and places marketable-LIMIT orders. Order/cancel return string ids.
"""
from __future__ import annotations

import logging

from typing import Optional

from ..exchange.contracts import ContractRegistry
from ..exchange.futures import KoinbayFutures
from .broker import Broker, Fill, Order, marketable_price

log = logging.getLogger("sentinel")


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, futures: KoinbayFutures, capital_override: Optional[float] = None,
                 stop_loss_pct: float = 0.0):
        self.fx = futures
        self.capital_override = capital_override
        self.stop_loss_pct = stop_loss_pct
        self._prepared: set[str] = set()

    # -- account -------------------------------------------------------------
    def _usdt_account(self) -> Optional[dict]:
        resp = self.fx.account()
        for a in (resp.get("account") or []):
            if a.get("marginCoin") == "USDT":
                return a
        return None

    @staticmethod
    def _num(v, default=0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def equity(self) -> float:
        a = self._usdt_account()
        if not a:
            return self.capital_override or 0.0
        eq = self._num(a.get("accountNormal")) + self._num(a.get("accountLock")) + self._num(a.get("unrealizedAmount"))
        if eq <= 0:
            eq = self._num(a.get("partEquity")) or (self.capital_override or 0.0)
        return eq

    def positions(self) -> dict[str, int]:
        a = self._usdt_account()
        out: dict[str, int] = {}
        if not a:
            return out
        for pv in (a.get("positionVos") or []):
            name = pv.get("contractName")
            signed = 0
            for p in (pv.get("positions") or []):
                vol = int(self._num(p.get("volume")))
                signed += vol if p.get("side") == "BUY" else -vol
            if name and signed != 0:
                out[name] = signed
        return out

    def preflight(self) -> None:
        """Authenticated read to confirm signing/keys before any order is placed."""
        if self._usdt_account() is None:
            raise RuntimeError("preflight failed: no USDT futures account returned (check API keys/permissions)")

    # -- setup + execution ---------------------------------------------------
    def prepare(self, symbols, registry: ContractRegistry, portfolio_cfg, exec_cfg) -> None:
        for sym in symbols:
            if sym in self._prepared or not registry.has(sym):
                continue
            spec = registry.get(sym)
            lev = max(1, min(portfolio_cfg.per_name_leverage, spec.max_lever))
            for call in (
                lambda: self.fx.edit_margin_mode(sym, exec_cfg.margin_model_code),
                lambda: self.fx.edit_position_mode(sym, exec_cfg.position_model_code),
                lambda: self.fx.edit_leverage(sym, lev),
            ):
                try:
                    call()
                except Exception:
                    pass  # already-set / not-permitted are non-fatal; order placement will surface real issues
            self._prepared.add(sym)

    def execute(self, orders: list[Order], registry: ContractRegistry, exec_cfg) -> list[Fill]:
        fills: list[Fill] = []
        for o in orders:
            spec = registry.get(o.symbol)
            if getattr(exec_cfg, "maker", False):
                price = spec.round_price(o.ref_price)   # rest at the touch; POST_ONLY (do not cross)
                tif, otype = "POST_ONLY", "LIMIT"
            elif exec_cfg.order_type == "limit":
                price = marketable_price(o.side, o.ref_price, exec_cfg.slippage_bps, spec)
                tif, otype = exec_cfg.time_in_force, "LIMIT"
            else:
                price, tif, otype = None, None, "MARKET"
            try:
                oid = self.fx.create_order(
                    contract=o.symbol, side=o.side, open_=o.open, type_=otype,
                    volume=o.volume, price=price, position_type=exec_cfg.margin_model_code,
                    time_in_force=tif,
                )
                fills.append(Fill(o.symbol, o.side, o.open, o.volume, price or o.ref_price, 0.0,
                                  order_id=oid, status="SUBMITTED"))
                if self.stop_loss_pct > 0 and o.open == "OPEN":
                    if not self._rest_stop(o, spec, exec_cfg):
                        # the position is open; its exchange-side brake is not. The 60s risk monitor
                        # still enforces position_stop_pct in software, so this is not fatal — but it
                        # means the position is unprotected whenever this process is not running, and
                        # that must never be silent.
                        fills[-1].status = "SUBMITTED:NO_STOP"
            except Exception as e:  # surface but don't abort the whole batch
                fills.append(Fill(o.symbol, o.side, o.open, o.volume, price or o.ref_price, 0.0,
                                  order_id="", status=f"ERROR:{e}"))
        return fills

    def _rest_stop(self, o: Order, spec, exec_cfg, retries: int = 1) -> bool:
        """Attach a resting catastrophe stop to a freshly opened position. Returns True if it rested.

        This used to swallow every failure with a bare `pass`, so a live position could exist with no
        exchange-side brake and nothing anywhere would say so. The software monitor still enforces
        position_stop_pct every 60s, which is why a failure here is not fatal — but it IS the
        protection that survives this process dying, so it gets a retry and an error, never silence.
        """
        frac = self.stop_loss_pct / 100.0
        if o.side == "BUY":   # long -> stop below, close with SELL
            trigger = spec.round_price(o.ref_price * (1 - frac)); close_side = "SELL"
        else:                 # short -> stop above, close with BUY
            trigger = spec.round_price(o.ref_price * (1 + frac)); close_side = "BUY"
        last = None
        for attempt in range(retries + 1):
            try:
                self.fx.create_stop_loss(o.symbol, close_side, o.volume, trigger,
                                         exec_cfg.margin_model_code)
                return True
            except Exception as exc:
                last = exc
        log.error("STOP NOT RESTED on %s %s %s @ trigger %s — position is open WITHOUT an "
                  "exchange-side stop; software monitor is the only brake until this is fixed: %s",
                  o.symbol, close_side, o.volume, trigger, last)
        return False
