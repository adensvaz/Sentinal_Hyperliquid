"""Smart-money signal: blend a basket of top Hyperliquid traders' net positioning.

For each wallet we read its live perp positions from Hyperliquid's public API and reduce them to a
**per-wallet normalized vector** (signed notional / that wallet's gross), so a $97M whale and a $16M
whale each contribute a unit of conviction — no single mega-wallet dominates the blend. Averaging those
vectors across the basket yields a consensus directional bias per coin in roughly [-1, +1]
(+1 = the whole basket is max-long that coin, -1 = max-short).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import httpx


@dataclass
class WhaleSnap:
    address: str
    equity: float
    gross: float
    positions: dict[str, float] = field(default_factory=dict)  # coin -> signed notional ($)

    @property
    def net(self) -> float:
        return sum(self.positions.values())


class WhaleTracker:
    def __init__(self, addresses: list[str], info_url: str = "https://api.hyperliquid.xyz/info",
                 weighting: str = "equal", timeout: float = 20.0):
        self.addresses = addresses
        self.info_url = info_url
        self.weighting = weighting
        self._http = httpx.Client(timeout=timeout)

    def _clearinghouse(self, addr: str) -> dict:
        return self._http.post(self.info_url, json={"type": "clearinghouseState", "user": addr}).json()

    def fetch(self) -> list[WhaleSnap]:
        snaps: list[WhaleSnap] = []
        for addr in self.addresses:
            try:
                st = self._clearinghouse(addr)
            except Exception:
                continue
            eq = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
            positions: dict[str, float] = defaultdict(float)
            gross = 0.0
            for p in st.get("assetPositions", []):
                pos = p.get("position", {})
                szi = float(pos.get("szi", 0) or 0)
                pv = float(pos.get("positionValue", 0) or 0)
                if pv == 0:
                    continue
                coin = str(pos.get("coin", "")).split(":")[-1].upper()  # strip HIP-3 'xyz:' prefix
                positions[coin] += pv if szi > 0 else -pv
                gross += pv
            if gross > 0:
                snaps.append(WhaleSnap(addr, eq, gross, dict(positions)))
        return snaps

    def blended_bias(self, snaps: list[WhaleSnap]) -> dict[str, float]:
        """Consensus directional bias per coin (HL coin symbol -> ~[-1, +1])."""
        agg: dict[str, float] = defaultdict(float)
        wsum = 0.0
        for s in snaps:
            w = s.equity if self.weighting == "equity" else 1.0
            if w <= 0 or s.gross <= 0:
                continue
            wsum += w
            for coin, signed in s.positions.items():
                agg[coin] += w * (signed / s.gross)  # this wallet's fraction-of-book in `coin`
        if wsum <= 0:
            return {}
        return {c: v / wsum for c, v in agg.items()}

    def close(self) -> None:
        self._http.close()


def map_bias_to_symbols(coin_bias: dict[str, float], coin_to_symbol: dict[str, str]) -> dict[str, float]:
    """Map HL-coin bias to KoinBay contractNames using {UNDERLYING: contractName}."""
    out: dict[str, float] = {}
    for coin, bias in coin_bias.items():
        sym = coin_to_symbol.get(coin.upper())
        if sym:
            out[sym] = bias
    return out
