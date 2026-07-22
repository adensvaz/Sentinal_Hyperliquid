"""Hyperliquid futures adapter.

Implements the same *read* surface as ``KoinbayFutures`` (contracts / ticker / index / klines) so the
engine, market-data layer and dashboard run on Hyperliquid **unchanged** — it's a drop-in, not a rewrite.

Why this is clean:
  * Hyperliquid's public ``info`` API returns the WHOLE universe (meta + per-asset contexts) in a single
    POST, so we fetch it once and cache it (TTL). That makes the HL adapter *faster* than KoinBay, which
    needs a call per symbol.
  * HL order sizes are in coin units with a per-coin ``szDecimals`` precision. We map ``szDecimals`` to a
    synthetic ``multiplier`` = 10**-szDecimals, so 1 "contract" == one size tick and the existing
    integer-contract sizing/registry math (contracts.py) works transparently.

Security / execution:
  * Everything here is READ-ONLY and needs NO keys (HL ``info`` is public). Paper trading works today.
  * The signed execution surface (create_order/cancel/positions/account/...) is deliberately **stubbed**.
    Live trading on HL signs orders with an EIP-712 wallet signature via the official
    ``hyperliquid-python-sdk`` and an **agent/API wallet** (trade-only, cannot withdraw) supplied through
    ``.env``. That is Phase 2, testnet-first, and double-opt-in — see ``_no_exec`` below.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("sentinel")

# KoinBay interval label -> Hyperliquid candle interval
_ITV = {"1day": "1d", "1d": "1d", "1week": "1w", "4h": "4h", "2h": "2h", "1h": "1h",
        "30min": "30m", "15min": "15m", "5min": "5m", "1min": "1m"}
_ITV_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
           "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}

# Hyperliquid base fee tier (fraction of notional). Real rate drops with 14-day volume + HYPE staking;
# this is the conservative base so backtests/sizing don't under-count cost.
HL_TAKER = 0.00045
HL_MAKER = 0.00015


class HyperliquidFutures:
    """Drop-in for KoinbayFutures against Hyperliquid's info API (read) + a gated exec stub."""

    funding_interval_hours = 1.0             # Hyperliquid funding settles HOURLY (KoinBay = 8h)

    def __init__(self, info_url: str = "https://api.hyperliquid.xyz/info",
                 timeout_s: float = 15.0, ttl_s: float = 8.0):
        self.info_url = info_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout_s)
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._ctx_ts = 0.0
        self._ctx: dict[str, dict] = {}      # coin -> {"meta": {...}, "ctx": {...}}

    # -- low-level info POST (public, unsigned) — retries on 429 / transient errors ------------
    def _post(self, body: dict, tries: int = 5) -> Any:
        last = None
        for k in range(tries):
            try:
                r = self._http.post(self.info_url, json=body)
                if r.status_code == 429:                    # rate limited -> back off and retry
                    time.sleep(0.4 * (k + 1)); continue
                r.raise_for_status()
                return r.json()
            except Exception as e:                          # transient network/5xx -> retry
                last = e
                time.sleep(0.3 * (k + 1))
        if last:
            raise last
        raise RuntimeError("hyperliquid info: rate-limited, retries exhausted")

    def _universe(self) -> dict[str, dict]:
        """metaAndAssetCtxs -> {coin: {meta, ctx}}, cached for ttl_s (thread-safe)."""
        now = time.time()
        with self._lock:
            if self._ctx and now - self._ctx_ts < self._ttl:
                return self._ctx
        meta, ctxs = self._post({"type": "metaAndAssetCtxs"})
        m: dict[str, dict] = {}
        for a, c in zip(meta.get("universe", []), ctxs):
            if a.get("isDelisted"):
                continue
            m[a["name"]] = {"meta": a, "ctx": c}
        with self._lock:
            self._ctx, self._ctx_ts = m, now
        return m

    # ===================== READ SURFACE (matches KoinbayFutures) =====================
    def contracts(self) -> list[dict]:
        out = []
        for name, d in self._universe().items():
            a, c = d["meta"], d["ctx"]
            szdec = int(a.get("szDecimals", 4) or 0)
            mult = 10.0 ** (-szdec)                                   # 1 contract == one size tick
            mark = float(c.get("markPx") or c.get("midPx") or 0.0)
            # HL prices carry ~5 significant figures; derive a safe decimal count for rounding
            intlen = len(str(int(abs(mark)))) if mark >= 1 else 1
            pxdec = max(0, 6 - intlen) if mark >= 1 else 6
            minvol = max(1, int(round(10.0 / (mark * mult)))) if mark > 0 else 1   # ~$10 HL min notional
            out.append({
                "symbol": name, "type": "E", "marginCoin": "USDC",
                "multiplier": mult, "multiplierCoin": name,
                "pricePrecision": pxdec, "minOrderVolume": minvol, "maxMarketVolume": 0,
                "minLever": 1, "maxLever": int(a.get("maxLeverage", 1) or 1), "status": 1,
                "openTakerFee": HL_TAKER, "closeTakerFee": HL_TAKER,
                "openMakerFee": HL_MAKER, "closeMakerFee": HL_MAKER,
            })
        return out

    def ticker(self, contract: str) -> dict:
        d = self._universe().get(contract)
        if not d:
            return {"last": 0.0, "vol": 0.0}
        c, a = d["ctx"], d["meta"]
        mark = float(c.get("markPx") or c.get("midPx") or 0.0)
        mult = 10.0 ** (-int(a.get("szDecimals", 4) or 0))
        ntl = float(c.get("dayNtlVlm") or 0.0)                        # HL day volume is USD notional
        # build_universe computes quote_notional = vol * multiplier * last; invert so it recovers dayNtlVlm
        vol = ntl / (mult * mark) if (mult > 0 and mark > 0) else 0.0
        return {"last": mark, "vol": vol}

    def index(self, contract: str) -> dict:
        d = self._universe().get(contract)
        if not d:
            return {"currentFundRate": 0.0, "nextFundRate": 0.0, "tagPrice": 0.0, "indexPrice": 0.0}
        c = d["ctx"]
        f = float(c.get("funding") or 0.0)                           # HL funding is HOURLY
        mark = float(c.get("markPx") or c.get("midPx") or 0.0)
        return {"currentFundRate": f, "nextFundRate": f, "tagPrice": mark,
                "indexPrice": float(c.get("oraclePx") or mark)}

    def klines(self, contract: str, interval: str, limit: int = 100) -> list[dict]:
        itv = _ITV.get(interval, "1d")
        ms = _ITV_MS.get(itv, 86_400_000)
        end = int(time.time() * 1000)
        start = end - (limit + 2) * ms
        rows = self._post({"type": "candleSnapshot",
                           "req": {"coin": contract, "interval": itv, "startTime": start, "endTime": end}})
        out = []
        for r in rows or []:
            try:
                c = float(r["c"])
                out.append({"idx": int(r["t"]), "close": c,
                            "open": float(r.get("o", c)), "high": float(r.get("h", c)),
                            "low": float(r.get("l", c)), "vol": float(r.get("v", 0) or 0)})
            except (KeyError, TypeError, ValueError):
                continue
        return out[-limit:]

    def ping(self) -> Any:
        self._post({"type": "meta"})
        return "ok"

    def server_time(self) -> int:
        return int(time.time() * 1000)

    def depth(self, contract: str, limit: int = 100) -> dict:
        return self._post({"type": "l2Book", "coin": contract})

    def close(self) -> None:
        self._http.close()

    # ===================== EXECUTION SURFACE (Phase 2 — gated, not enabled) =====================
    def _no_exec(self, *a, **k):
        raise NotImplementedError(
            "Hyperliquid execution is Phase 2 and not enabled. It requires an AGENT/API wallet "
            "(trade-only, cannot withdraw) in .env as HL_ACCOUNT_ADDRESS + HL_AGENT_KEY, plus the "
            "official hyperliquid-python-sdk for EIP-712 order signing. Validate on testnet first; "
            "going live is double-opt-in. Read-only data and PAPER trading work today without any key."
        )

    # signed methods — same names/signatures as KoinbayFutures, all gated until Phase 2
    create_order = _no_exec
    cancel_order = _no_exec
    query_order = _no_exec
    open_orders = _no_exec
    positions = _no_exec
    account = _no_exec
    my_trades = _no_exec
    edit_leverage = _no_exec
    edit_margin_mode = _no_exec
    edit_position_mode = _no_exec
    create_stop_loss = _no_exec
