"""Execution policy that learns which way to send an order, and keeps learning.

WHY THIS EXISTS, AND WHY IT IS NOT A REGIME MODEL
-------------------------------------------------
The live Carry ledger settled the question of where the money goes: 203 closed trades over 38.8
days produced a gross trading P&L of -$114.51 (i.e. the signal traded flat) while fees took
$139.71 — 13.2%/yr of capital. The loss is execution, not selection.

A regime model was tested against that and rejected: across 900 days, eight backward-looking
observables were regressed on each book's forward 21-day return and NOT ONE cleared |t| > 2. The
effective sample is only ~37 independent windows, so there is nothing there to learn and a model
fitted to it would be memorising noise.

Execution is the opposite case. It is genuinely partially observable — you cannot see true depth,
who is on the other side, or whether your resting order is about to be adversely selected — and it
produces one labelled observation per order, roughly 16 per rebalance rather than 37 per 900 days.
That is the only place in this system where the data rate supports learning.

WHAT IT LEARNS
--------------
For each (context, arm) it estimates the DISTRIBUTION of execution cost in basis points versus the
arrival mid, then Thompson-samples to choose. Thompson sampling matters here for a specific reason:
the classic way an execution learner silently fails is that it always picks one arm and therefore
never observes the others. Sampling from the posterior explores exactly in proportion to remaining
uncertainty, with no epsilon to tune and no exploration schedule to get wrong.

THE SUBTLETY THAT BREAKS NAIVE IMPLEMENTATIONS
----------------------------------------------
An unfilled passive order does not cost zero. You still need the position, so the true cost of
POST is (probability of fill x price improvement) + (probability of miss x the cost of completing
later, at a price that has usually moved against you). Scoring a miss as 0 makes POST look free and
the policy degenerates into never crossing, which is how books end up chronically under-filled and
chasing. `record` therefore requires the COMPLETED cost of the parent decision, not the fill price.

FORGETTING
----------
Microstructure drifts. Sufficient statistics decay with a configurable half-life (default 45 days)
so the posterior tracks the current market instead of averaging over a regime that has ended. This
is what makes it "continuously" learning rather than "trained once".
"""
from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field, asdict

# --------------------------------------------------------------------------------------- arms
POST = "POST"       # rest passively at the touch; may not fill
CROSS = "CROSS"     # take liquidity now; certain fill, pays the spread
PEG = "PEG"         # rest briefly, then cross if unfilled — the hedge between the two
ARMS = (POST, CROSS, PEG)

# Physics priors, in basis points of cost (negative = we were paid). These are not guesses about
# the market; they are the mechanical fee structure. Hyperliquid maker ~1.5bps, taker ~4.5bps.
# The policy starts here and moves only as far as evidence justifies.
#
# POST was 1.0 — the 1.5bps maker fee less an assumed 0.5bps of captured spread. That assumption is
# wrong. A live experiment on the Binance BTC perpetual (arXiv:2502.18625) found maker fill
# likelihood NEGATIVELY correlated with post-fill returns: a resting order fills precisely when the
# market is coming through its level, so on average it does not capture the spread it appears to.
# Set to the bare maker fee, which is the only part of POST that is genuinely mechanical. The
# policy can still learn a better number — but it should have to earn it rather than start there.
PRIOR_COST_BPS = {POST: 1.5, CROSS: 5.5, PEG: 3.0}
PRIOR_STRENGTH = 8.0        # prior counts as this many observations; keeps early behaviour sane
PRIOR_SD_BPS = 6.0          # expected spread of per-order costs around the mean


def _bucket_hour(ts: float) -> str:
    """UTC hour bucket. Measured on 60 days of HL hourly bars: 13:00 UTC averages 0.869% hourly
    movement at 1.94x baseline volume, versus 0.547% at 07:00. Liquidity is genuinely not uniform,
    so the hour is real context rather than a feature added for its own sake."""
    h = time.gmtime(ts).tm_hour
    if 12 <= h <= 16:
        return "us_open"       # deepest book, tightest spread
    if 17 <= h <= 21:
        return "us_pm"
    if 7 <= h <= 11:
        return "europe"        # thinnest of the four
    return "asia"


def _bucket(x: float, edges: tuple) -> int:
    for i, e in enumerate(edges):
        if x < e:
            return i
    return len(edges)


@dataclass
class Cell:
    """Decaying Gaussian sufficient statistics for one (context, arm) pair."""
    n: float = 0.0          # effective observation count, after decay
    mean: float = 0.0       # running mean cost in bps
    m2: float = 0.0         # running sum of squared deviations, for variance
    last_ts: float = 0.0

    def decay(self, now: float, half_life_days: float) -> None:
        """Exponentially forget. Applied lazily so cold cells cost nothing to maintain."""
        if self.last_ts <= 0 or self.n <= 0:
            self.last_ts = now
            return
        dt_days = max(0.0, (now - self.last_ts) / 86_400.0)
        if dt_days > 0:
            k = 0.5 ** (dt_days / max(1e-9, half_life_days))
            self.n *= k
            self.m2 *= k
        self.last_ts = now

    def update(self, cost_bps: float) -> None:
        self.n += 1.0
        d = cost_bps - self.mean
        self.mean += d / self.n
        self.m2 += d * (cost_bps - self.mean)

    @property
    def sd(self) -> float:
        if self.n < 2:
            return PRIOR_SD_BPS
        return max(0.5, math.sqrt(max(0.0, self.m2 / self.n)))


class ExecutionPolicy:
    """Contextual Thompson-sampling bandit over execution arms, with guardrails.

    Deliberately small: ~4 hour buckets x 3 spread buckets x 3 volatility buckets x 3 arms = 108
    cells. Carry alone generates ~16 orders a rebalance, so cells fill in weeks. A neural policy
    over the same data would carry more parameters than observations — the reason this is a bandit
    and not a network is arithmetic, not preference.
    """

    SPREAD_EDGES = (3.0, 8.0)       # bps: tight / normal / wide
    VOL_EDGES = (0.03, 0.06)        # daily sigma: calm / normal / volatile

    def __init__(self, half_life_days: float = 45.0, min_obs: float = 5.0,
                 max_cost_bps: float = 25.0, explore: bool = True, seed: int | None = None):
        self.cells: dict[str, Cell] = {}
        self.half_life_days = half_life_days
        self.min_obs = min_obs              # below this, lean on the physics prior
        self.max_cost_bps = max_cost_bps    # guardrail: never accept a worse expectation than this
        self.explore = explore
        self.rng = random.Random(seed)
        self.decisions = 0

    # ------------------------------------------------------------------ context
    def context(self, *, ts: float | None = None, spread_bps: float = 5.0,
                daily_vol: float = 0.04, urgent: bool = False) -> str:
        ts = ts if ts is not None else time.time()
        return (f"{_bucket_hour(ts)}|s{_bucket(spread_bps, self.SPREAD_EDGES)}"
                f"|v{_bucket(daily_vol, self.VOL_EDGES)}|{'U' if urgent else 'N'}")

    def _cell(self, ctx: str, arm: str, now: float) -> Cell:
        key = f"{ctx}|{arm}"
        c = self.cells.get(key)
        if c is None:
            c = Cell(last_ts=now)
            self.cells[key] = c
        c.decay(now, self.half_life_days)
        return c

    # ------------------------------------------------------------------ decide
    def posterior(self, ctx: str, arm: str, now: float | None = None) -> tuple[float, float]:
        """Posterior mean and standard error of cost for this (context, arm), prior-blended."""
        now = now if now is not None else time.time()
        c = self._cell(ctx, arm, now)
        p0, k0 = PRIOR_COST_BPS[arm], PRIOR_STRENGTH
        n_eff = c.n + k0
        mean = (c.mean * c.n + p0 * k0) / n_eff
        se = c.sd / math.sqrt(n_eff)
        return mean, se

    def choose(self, ctx: str, allowed: tuple = ARMS, now: float | None = None) -> str:
        """Thompson sample: draw a plausible cost for each arm, take the cheapest draw.

        Exploration is automatic — an arm with few observations has a wide posterior and therefore
        keeps getting sampled until the evidence narrows it. Nothing to schedule, nothing to decay.
        """
        now = now if now is not None else time.time()
        self.decisions += 1
        best, best_draw = None, None
        for arm in allowed:
            mean, se = self.posterior(ctx, arm, now)
            draw = self.rng.gauss(mean, se) if self.explore else mean
            if mean > self.max_cost_bps:        # guardrail: refuse a clearly terrible arm outright
                continue
            if best_draw is None or draw < best_draw:
                best, best_draw = arm, draw
        return best or CROSS                    # crossing always completes; safe fallback

    # ------------------------------------------------------------------ learn
    def record(self, ctx: str, arm: str, cost_bps: float, now: float | None = None) -> None:
        """Learn from ONE completed parent decision.

        `cost_bps` must be the cost of finishing the job, signed so that positive = we paid:
            (fill_vwap / arrival_mid - 1) * 1e4   for a BUY
            (1 - fill_vwap / arrival_mid) * 1e4   for a SELL
        For POST that missed and was completed later, this is the cost of the WHOLE sequence
        including the chase. Scoring a miss as zero is the mistake that makes a learner conclude
        passive orders are free.
        """
        now = now if now is not None else time.time()
        cost_bps = max(-self.max_cost_bps, min(self.max_cost_bps, float(cost_bps)))  # clip outliers
        self._cell(ctx, arm, now).update(cost_bps)

    # ------------------------------------------------------------------ introspection
    def table(self) -> list[dict]:
        now = time.time()
        rows = []
        for key in sorted(self.cells):
            ctx, arm = key.rsplit("|", 1)
            mean, se = self.posterior(ctx, arm, now)
            rows.append(dict(context=ctx, arm=arm, n=round(self.cells[key].n, 1),
                             mean_bps=round(mean, 2), se_bps=round(se, 2)))
        return rows

    def drift_alert(self, ctx: str, arm: str, recent_cost_bps: float,
                    z: float = 3.0) -> str | None:
        """Has reality diverged from what we believe? Silent drift is the main way a learned
        policy rots — it keeps choosing confidently while the world moves underneath it."""
        mean, se = self.posterior(ctx, arm)
        if se <= 0:
            return None
        if abs(recent_cost_bps - mean) > z * max(se, 1.0):
            return (f"execution drift: {ctx}/{arm} realised {recent_cost_bps:+.1f}bps vs "
                    f"expected {mean:+.1f}±{se:.1f}bps")
        return None

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> str:
        return json.dumps({"half_life_days": self.half_life_days,
                           "cells": {k: asdict(v) for k, v in self.cells.items()}})

    @classmethod
    def from_json(cls, blob: str, **kw) -> "ExecutionPolicy":
        p = cls(**kw)
        try:
            data = json.loads(blob or "{}")
        except (TypeError, ValueError):
            return p
        p.half_life_days = float(data.get("half_life_days", p.half_life_days))
        for k, v in (data.get("cells") or {}).items():
            p.cells[k] = Cell(**v)
        return p


def realised_cost_bps(side: str, fill_price: float, arrival_mid: float) -> float:
    """Signed execution cost in bps: positive means the fill was worse than the arrival mid.

    This is the ONLY honest scorecard for execution. Measuring against the fill price itself is
    circular, and measuring against the close bakes in the market's drift rather than our own
    slippage.
    """
    if arrival_mid <= 0 or fill_price <= 0:
        return 0.0
    raw = (fill_price / arrival_mid - 1.0) * 10_000.0
    return raw if side.upper() == "BUY" else -raw
