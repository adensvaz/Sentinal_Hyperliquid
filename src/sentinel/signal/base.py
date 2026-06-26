"""SignalProvider interface and the per-symbol input it consumes.

The provider is PURE: it receives pre-fetched market data (so it's trivially unit-testable) and returns a
continuous score per symbol. Swap MarketProxySignal for any other SignalProvider (e.g. a real social
sentiment feed) without touching the strategy/execution layers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SymbolData:
    """Everything the signal needs about one contract, pre-fetched from KoinBay."""
    symbol: str                       # contractName, e.g. "E-BTC-USDT"
    idx_ms: list[int] = field(default_factory=list)   # ascending kline open-times (ms)
    closes: list[float] = field(default_factory=list)  # close per bar, aligned to idx_ms
    volumes: list[float] = field(default_factory=list)  # volume per bar, aligned to idx_ms
    funding: float = 0.0              # currentFundRate from /fapi/v1/index
    next_funding: float = 0.0         # nextFundRate (forward) — funding velocity = next - current
    last_price: float = 0.0           # latest mark/last price

    @property
    def n_bars(self) -> int:
        return len(self.closes)


class SignalProvider(ABC):
    """Maps {symbol: SymbolData} -> {symbol: continuous score} (higher = more bullish)."""

    @abstractmethod
    def scores(self, data: dict[str, SymbolData]) -> dict[str, float]:
        ...

    # interval the engine should fetch klines at, and how much history (bars) to request
    base_interval: str = "4h"
    history_bars: int = 120
