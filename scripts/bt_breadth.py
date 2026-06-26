"""#27 A/B: universe breadth (top_n 24->32) x weight rebalance (volume 0.1->0, funding 0.2->0.3).
HONEST CAVEAT: funding & whale are inert in the kline backtest (no historical funding/whale data), so
the only ACTIVE factors here are momentum + volume. The weight test therefore measures whether dropping
the (collinear) volume factor helps. Run: ./.venv/bin/python scripts/bt_breadth.py"""
import copy
import sys
import time as _t

sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import load_history, simulate


def monthly(curve):
    b = {}
    for ts, eq, _g in curve:
        ym = _t.strftime("%Y-%m", _t.localtime(ts / 1000))
        b.setdefault(ym, [eq, eq])[1] = eq
    return {ym: (v[1] / v[0] - 1) * 100 for ym, v in sorted(b.items())}


def worst(curve):
    m = monthly(curve)
    return min(m.values()) if m else 0.0


def negm(curve):
    return sum(1 for v in monthly(curve).values() if v < 0)


base = load_config()
base.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
maker = base.execution.maker
fee = 0.00025 if maker else 0.00075
slip = 2.0 if maker else base.execution.slippage_bps


def reweight(cfg):
    cfg.signal.weights.volume = 0.0
    cfg.signal.weights.funding = 0.3
    return cfg


# two history loads (top_n changes the fetched universe)
cfg24 = copy.deepcopy(base); cfg24.universe.top_n = 24
cfg32 = copy.deepcopy(base); cfg32.universe.top_n = 32
hist24 = load_history(cfg24, interval="1day", limit=300)
hist32 = load_history(cfg32, interval="1day", limit=300)
print(f"top_n=24 -> {len(hist24['universe'])} coins ; top_n=32 -> {len(hist32['universe'])} coins\n")


def run(hist, cfg):
    return simulate(hist, cfg, rebalance_every=1, taker_fee=fee, slippage_bps=slip)


scen = [
    ("base (24, vol on)", run(hist24, copy.deepcopy(base))),
    ("reweight (24)", run(hist24, reweight(copy.deepcopy(base)))),
    ("breadth (32)", run(hist32, copy.deepcopy(base))),
    ("both (32+reweight)", run(hist32, reweight(copy.deepcopy(base)))),
]
w = 20
print(f"{'metric':<20}" + "".join(f"{n:>{w}}" for n, _ in scen))
print("-" * (20 + w * len(scen)))


def row(label, fn):
    print(f"{label:<20}" + "".join(f"{fn(m):>{w}}" for _, m in scen))


row("net return %", lambda m: f"{m['net_return_pct']:+.1f}")
row("Sharpe", lambda m: f"{m['sharpe']:.2f}")
row("max drawdown %", lambda m: f"{m['max_dd_pct']:.1f}")
row("worst month %", lambda m: f"{worst(m['curve']):+.1f}")
row("neg months", lambda m: f"{negm(m['curve'])}")
row("hit rate %", lambda m: f"{m['hit_rate_pct']:.0f}")
row("avg turnover x", lambda m: f"{m['avg_turnover_x']:.2f}")
