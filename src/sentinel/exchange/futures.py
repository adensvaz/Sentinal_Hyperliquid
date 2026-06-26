"""Typed wrapper over the KoinBay futures REST endpoints (/fapi/v1/...).

Endpoint shapes and quirks are documented in docs/KOINBAY_API.md. Notable ones handled here:
- create/cancel return orderId as a STRING (kept as str).
- openOrders returns {"code","msg","data"} where empty == data:null (not []).
- positions requires contractName; account returns balance + all positions in one call.
"""
from __future__ import annotations

from typing import Any, Optional

from .client import KoinbayClient


class KoinbayFutures:
    def __init__(self, client: KoinbayClient):
        self.c = client

    # -- public market data --------------------------------------------------
    def ping(self) -> Any:
        return self.c.get("/fapi/v1/ping")

    def server_time(self) -> int:
        return int(self.c.get("/fapi/v1/time")["serverTime"])

    def contracts(self) -> list[dict]:
        return self.c.get("/fapi/v1/contracts")

    def depth(self, contract: str, limit: int = 100) -> dict:
        return self.c.get("/fapi/v1/depth", {"contractName": contract, "limit": limit})

    def ticker(self, contract: str) -> dict:
        return self.c.get("/fapi/v1/ticker", {"contractName": contract})

    def index(self, contract: str) -> dict:
        """Funding + mark/index: {currentFundRate, tagPrice, indexPrice, nextFundRate}."""
        return self.c.get("/fapi/v1/index", {"contractName": contract})

    def klines(self, contract: str, interval: str, limit: int = 100) -> list[dict]:
        return self.c.get(
            "/fapi/v1/klines",
            {"contractName": contract, "interval": interval, "limit": limit},
        )

    # -- trading (signed) ----------------------------------------------------
    def create_order(
        self,
        contract: str,
        side: str,           # BUY | SELL
        open_: str,          # OPEN | CLOSE
        type_: str,          # LIMIT | MARKET
        volume: float,       # contracts
        price: float | None = None,
        position_type: int = 2,        # 1 cross | 2 isolated
        time_in_force: str | None = None,
        client_order_id: str | None = None,
    ) -> str:
        body = {
            "contractName": contract,
            "side": side,
            "open": open_,
            "type": type_,
            "volume": volume,
            "price": price,
            "positionType": position_type,
            "timeInForce": time_in_force,
            "clientOrderId": client_order_id,
        }
        resp = self.c.post("/fapi/v1/order", body, signed=True)
        return str(resp["orderId"])

    def cancel_order(self, contract: str, order_id: str) -> str:
        resp = self.c.post("/fapi/v1/cancel", {"contractName": contract, "orderId": order_id}, signed=True)
        return str(resp["orderId"])

    def query_order(self, contract: str, order_id: str) -> dict:
        return self.c.get("/fapi/v1/order", {"contractName": contract, "orderId": order_id}, signed=True)

    def open_orders(self, contract: str | None = None) -> list[dict]:
        params = {"contractName": contract} if contract else None
        resp = self.c.get("/fapi/v1/openOrders", params, signed=True)
        # success envelope; empty == data:null
        data = resp.get("data") if isinstance(resp, dict) else None
        return data or []

    def positions(self, contract: str) -> list[dict]:
        resp = self.c.get("/fapi/v1/positions", {"contractName": contract}, signed=True)
        return resp.get("positions", []) if isinstance(resp, dict) else []

    def account(self) -> dict:
        return self.c.get("/fapi/v1/account", signed=True)

    def my_trades(self, contract: str, limit: int = 100) -> list[dict]:
        return self.c.get("/fapi/v1/myTrades", {"contractName": contract, "limit": str(limit)}, signed=True)

    # -- per-contract setup (signed) -----------------------------------------
    def edit_leverage(self, contract: str, level: int) -> Any:
        return self.c.post("/fapi/v1/edit_lever", {"contractName": contract, "nowLevel": str(level)}, signed=True)

    def edit_margin_mode(self, contract: str, margin_model: int) -> Any:
        """margin_model: 1 cross | 2 isolated."""
        return self.c.post(
            "/fapi/v1/edit_user_margin_model",
            {"contractName": contract, "marginModel": margin_model},
            signed=True,
        )

    def edit_position_mode(self, contract: str, position_model: int) -> Any:
        """position_model: 1 net | 2 two-way."""
        return self.c.post(
            "/fapi/v1/edit_user_position_model",
            {"contractName": contract, "positionModel": position_model},
            signed=True,
        )

    def create_stop_loss(self, contract: str, side: str, volume: int, trigger_price: float,
                         position_type: int = 2) -> Optional[str]:
        """Resting stop-loss (a CLOSE trigger) via /fapi/v1/conditionOrder/ (triggerType=1).
        `side` is the CLOSE side (SELL to close a long, BUY to close a short). Returns triggerId."""
        body = {"contractName": contract, "side": side, "open": "CLOSE", "type": "MARKET",
                "volume": int(volume), "triggerType": 1, "triggerPrice": str(trigger_price),
                "positionType": position_type}
        resp = self.c.post("/fapi/v1/conditionOrder/", body, signed=True)
        data = resp.get("data") if isinstance(resp, dict) else None
        ids = (data or {}).get("triggerIds") or []
        return str(ids[0]) if ids else None
