"""Real order-book observation, so the execution policy can learn in PAPER without fake data.

THE PROBLEM THIS SOLVES
-----------------------
Paper fills are synthetic: paper_broker blends `maker_fill_ratio * touch + (1-ratio) * taker`.
Training the policy on those teaches it the value of a config constant, not the market. That is
circular and worthless.

The order book, however, is real in paper mode — it is the same l2Book every live trader sees. So
the honest split is:

  measurable from the real book, right now      -> spread, depth, the cost of crossing for OUR size
  measurable from real price action, next cycle -> whether a resting order would have filled
  NOT measurable in paper                       -> queue position, our own market impact

Everything the policy learns in paper comes from the first two. Nothing comes from the simulated
fill. That makes paper training legitimate rather than a rehearsal against ourselves.

WHY THIS MATTERS AT ALL, measured on live HL depth for a $625 order (the per-name size at
gross 1.0 with top_k 8):
    BTC    spread 0.16bps   top-5 depth $669,467   cost to cross 0.08bps
    ZRO    spread 1.28bps   top-5 depth   $3,950   cost to cross 0.64bps
    KAITO  spread 2.01bps   top-5 depth     $823   cost to cross 2.72bps
A 34x range in true cost, against a single `slippage_bps: 5` applied to all three. The flat
assumption is wrong in both directions, and only the book can say by how much.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("sentinel")


@dataclass
class BookState:
    """One observation of the real book. All costs in basis points of the mid."""
    mid: float
    spread_bps: float
    top_depth_usd: float          # resting notional on the side we would take
    cross_bps: float              # true cost of crossing for OUR size, by walking levels
    levels: int
    thin: bool                    # our order is large relative to visible depth

    @property
    def half_spread_bps(self) -> float:
        return self.spread_bps / 2.0


def _levels(book: dict) -> tuple[list, list]:
    lv = (book or {}).get("levels") or []
    if len(lv) < 2:
        return [], []
    return lv[0] or [], lv[1] or []          # bids, asks


def probe(fx, symbol: str, side: str, notional: float, depth_levels: int = 5) -> BookState | None:
    """Read the live book and price OUR order against it. Returns None if the book is unusable.

    `side` is the order side: a BUY consumes asks, a SELL consumes bids.
    """
    try:
        book = fx.depth(symbol)
        bids, asks = _levels(book)
        if not bids or not asks:
            return None
        best_bid, best_ask = float(bids[0]["px"]), float(asks[0]["px"])
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        mid = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid * 10_000.0

        take = asks if side.upper() == "BUY" else bids
        sign = 1.0 if side.upper() == "BUY" else -1.0

        # Walk the book for our actual size. This is the true cost of crossing, not an assumption.
        need, got, cost = abs(float(notional)), 0.0, 0.0
        for lvl in take:
            px, sz = float(lvl["px"]), float(lvl["sz"])
            avail = px * sz
            if avail <= 0:
                continue
            chunk = min(need - got, avail)
            cost += chunk * sign * (px / mid - 1.0)
            got += chunk
            if got >= need:
                break
        if got <= 0:
            return None
        # If the book cannot absorb us at all, price the remainder at the worst level we saw and
        # flag it. Silently pricing an unfillable order at the touch is how backtests lie.
        if got < need:
            worst = float(take[-1]["px"])
            cost += (need - got) * sign * (worst / mid - 1.0)
        cross_bps = cost / need * 10_000.0

        top_depth = sum(float(l["px"]) * float(l["sz"]) for l in take[:depth_levels])
        return BookState(mid=mid, spread_bps=spread_bps, top_depth_usd=top_depth,
                         cross_bps=max(0.0, cross_bps), levels=len(take),
                         thin=(top_depth < need * 3.0))
    except Exception as e:                                    # noqa: BLE001
        log.debug("book probe failed for %s: %s", symbol, e)
        return None


def post_outcome_bps(side: str, touch_price: float, arrival_mid: float,
                     low: float, high: float, completion_bps: float,
                     post_mid: float = 0.0, adverse_cap_bps: float = 30.0) -> tuple[float, bool]:
    """Score a resting order from REAL subsequent price action.

    A passive BUY at `touch_price` fills only if the market actually traded down to it. If it did
    not, we still need the position, so the cost is `completion_bps`: what it then took to cross,
    measured on the book at that later moment. Returning the completion cost rather than zero is
    the entire point — an unfilled passive order is not free, it is deferred and usually more
    expensive, and a learner told otherwise concludes that posting is costless and stops crossing.

    ADVERSE SELECTION
    -----------------
    A resting order does not fill at random. It fills precisely when the market comes to it, and a
    live trading experiment on the Binance BTC perpetual (arXiv:2502.18625) measured fill
    likelihood to be NEGATIVELY correlated with post-fill returns: the same order-book conditions
    that get you filled are the ones that predict the price continuing through your level.

    So crediting a filled passive order with the full half-spread — which is what this function
    used to do — systematically overstates it. By the time the fill happened, fair value had
    already moved to our price; we did not buy below the market, we bought AT a market that was on
    its way down.

    `post_mid` supplies a later mid so that drift can be charged. The drift is applied SIGNED and
    symmetric, not clipped to the adverse side: the bandit needs an unbiased estimate of
    E[cost | filled], and charging only the bad half would bias it pessimistic and push the policy
    to cross when it should not. `adverse_cap_bps` bounds the magnitude so a single violent bar
    cannot dominate a cell's posterior. Passing post_mid=0 keeps the old optimistic behaviour.
    """
    if arrival_mid <= 0 or touch_price <= 0:
        return 0.0, False
    buy = side.upper() == "BUY"
    filled = (low <= touch_price) if buy else (high >= touch_price)
    if not filled:
        return float(completion_bps), False
    raw = (touch_price / arrival_mid - 1.0) * 10_000.0
    cost = raw if buy else -raw
    if post_mid > 0:
        drift = (post_mid / arrival_mid - 1.0) * 10_000.0
        adverse = -drift if buy else drift        # >0 = the market kept moving against the fill
        cost += max(-adverse_cap_bps, min(adverse_cap_bps, adverse))
    return cost, True
