"""Contract-spec registry + notional<->contracts math.

KoinBay futures order `volume` is a count of CONTRACTS, and notional = volume * multiplier * price
(e.g. E-BTC-USDT multiplier 0.0001 => 1 contract = 0.0001 BTC). All sizing must go through here so we
respect each contract's multiplier, price precision, and minimum/maximum order volume.
"""
from __future__ import annotations

from dataclasses import dataclass


def _f(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ContractSpec:
    name: str            # contractName, e.g. "E-BTC-USDT"
    type: str            # margin coin code; "E" == USDT-margined
    margin_coin: str
    multiplier: float    # underlying units per contract
    multiplier_coin: str
    price_precision: int
    min_order_volume: int
    max_market_volume: int
    min_lever: int
    max_lever: int
    status: int
    open_taker_fee: float = 0.0006
    close_taker_fee: float = 0.0006
    open_maker_fee: float = 0.00025
    close_maker_fee: float = 0.00025

    @property
    def is_active_usdt(self) -> bool:
        return self.type == "E" and self.status == 1

    @property
    def price_step(self) -> float:
        return 10.0 ** (-self.price_precision)

    @classmethod
    def from_raw(cls, d: dict) -> "ContractSpec":
        return cls(
            name=str(d.get("symbol") or d.get("contractName")),
            type=str(d.get("type", "")),
            margin_coin=str(d.get("marginCoin", "")),
            multiplier=_f(d, "multiplier", 1.0),
            multiplier_coin=str(d.get("multiplierCoin", "")),
            price_precision=int(d.get("pricePrecision", 2) or 0),
            min_order_volume=int(_f(d, "minOrderVolume", 1.0)) or 1,
            max_market_volume=int(_f(d, "maxMarketVolume", 0.0)),
            min_lever=int(_f(d, "minLever", 0.0)),
            max_lever=int(_f(d, "maxLever", 0.0)) or 1,
            status=int(d.get("status", 0) or 0),
            open_taker_fee=_f(d, "openTakerFee", 0.0006),
            close_taker_fee=_f(d, "closeTakerFee", 0.0006),
            open_maker_fee=_f(d, "openMakerFee", 0.00025),
            close_maker_fee=_f(d, "closeMakerFee", 0.00025),
        )

    # -- conversions ---------------------------------------------------------
    def round_price(self, price: float) -> float:
        return round(round(price / self.price_step) * self.price_step, self.price_precision)

    def notional_to_contracts(self, notional: float, price: float) -> int:
        """Signed notional (USDT) -> signed integer contract count, snapped to the lot size.
        Returns 0 if the magnitude is below the minimum order volume."""
        if price <= 0 or self.multiplier <= 0:
            return 0
        step = self.min_order_volume
        sign = 1 if notional >= 0 else -1
        raw = abs(notional) / (price * self.multiplier)
        vol = int(round(raw / step) * step)
        if vol < step:
            return 0
        if self.max_market_volume:
            vol = min(vol, self.max_market_volume)
        return sign * vol

    def contracts_to_notional(self, volume: float, price: float) -> float:
        return abs(volume) * price * self.multiplier


class ContractRegistry:
    def __init__(self, raw_contracts: list[dict]):
        specs = [ContractSpec.from_raw(c) for c in raw_contracts]
        self._by_name: dict[str, ContractSpec] = {s.name: s for s in specs}

    @classmethod
    def from_futures(cls, futures) -> "ContractRegistry":  # futures: KoinbayFutures
        return cls(futures.contracts())

    def get(self, name: str) -> ContractSpec:
        return self._by_name[name]

    def has(self, name: str) -> bool:
        return name in self._by_name

    def active_usdt(self) -> list[ContractSpec]:
        return [s for s in self._by_name.values() if s.is_active_usdt]

    def all(self) -> list[ContractSpec]:
        return list(self._by_name.values())
