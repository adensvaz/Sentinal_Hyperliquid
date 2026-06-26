"""Execution abstraction: Order/Fill value types, the marketable-limit price helper, and the Broker ABC.

Position math convention used everywhere: a BUY adds +volume to the signed position, a SELL adds
-volume — regardless of OPEN/CLOSE. So `signed_delta = +volume if side == 'BUY' else -volume`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..exchange.contracts import ContractRegistry, ContractSpec


@dataclass
class Order:
    symbol: str          # contractName
    side: str            # "BUY" | "SELL"
    open: str            # "OPEN" | "CLOSE"
    volume: int          # positive contract count
    ref_price: float     # current mark/last used to derive a marketable price
    notional: float      # abs USDT notional (for logging / min-notional checks)

    @property
    def signed_delta(self) -> int:
        return self.volume if self.side == "BUY" else -self.volume


@dataclass
class Fill:
    symbol: str
    side: str
    open: str
    volume: int
    price: float         # fill price
    fee: float           # USDT
    order_id: str = ""
    status: str = "FILLED"


def marketable_price(side: str, ref_price: float, slippage_bps: float, spec: ContractSpec) -> float:
    """Cross the spread by `slippage_bps` so a LIMIT order fills immediately, rounded to tick."""
    adj = slippage_bps / 10_000.0
    raw = ref_price * (1 + adj) if side == "BUY" else ref_price * (1 - adj)
    return spec.round_price(raw)


class Broker(ABC):
    mode: str = "abstract"

    @abstractmethod
    def equity(self) -> float:
        """Account equity in USDT (real for live, virtual for paper)."""

    @abstractmethod
    def positions(self) -> dict[str, int]:
        """Current signed contract count per contractName (+long / -short)."""

    @abstractmethod
    def execute(self, orders: list[Order], registry: ContractRegistry, exec_cfg) -> list[Fill]:
        """Send/simulate the orders and return fills."""

    def prepare(self, symbols: list[str], registry: ContractRegistry, portfolio_cfg, exec_cfg) -> None:
        """Per-contract setup (leverage/margin/position mode). No-op for paper."""
        return None
