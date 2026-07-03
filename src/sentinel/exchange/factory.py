"""Exchange factory — selects the futures adapter from ``cfg.exchange.venue``.

Both adapters expose the same read surface (contracts / ticker / index / klines), so the engine,
market-data and dashboard are venue-agnostic. Execution is live on KoinBay today and Phase-2 (gated)
on Hyperliquid; PAPER trading works on either venue with no keys.
"""
from __future__ import annotations


def make_futures(cfg):
    """Return a futures adapter (KoinbayFutures | HyperliquidFutures) for the configured venue."""
    venue = getattr(cfg.exchange, "venue", "koinbay")
    if venue == "hyperliquid":
        from .hyperliquid import HyperliquidFutures
        return HyperliquidFutures(cfg.exchange.hyperliquid_info_url, cfg.exchange.timeout_s)
    from .client import KoinbayClient
    from .futures import KoinbayFutures
    return KoinbayFutures(KoinbayClient(cfg.exchange.futures_host, cfg.api_key, cfg.api_secret,
                                        timeout_s=cfg.exchange.timeout_s))
