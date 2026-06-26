"""Sweep the untested knobs on one daily-history load to answer 'is the current config optimal?'.
Current live settings: conviction 1.5 · 5+5 names · daily rebalance. Run: ./.venv/bin/python scripts/bt_sweep.py"""
import copy, sys, time as _t
sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import load_history, simulate

base = load_config()
base.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
maker = base.execution.maker
fee = 0.00025 if maker else 0.00075
slip = 2.0 if maker else base.execution.slippage_bps
hist = load_history(base, interval="1day", limit=300)
print(f"daily history: {len(hist['T'])} bars, {len(hist['universe'])} coins  (one shared load)\n")


def worst_month(curve):
    b = {}
    for ts, eq, _g in curve:
        ym = _t.strftime("%Y-%m", _t.localtime(ts / 1000))
        b.setdefault(ym, [eq, eq])[1] = eq
    return min(((v[1] / v[0] - 1) * 100 for v in b.values()), default=0.0)


def run(cfg, rebal=1):
    return simulate(hist, cfg, rebalance_every=rebal, taker_fee=fee, slippage_bps=slip)


def row(label, m, star=""):
    print(f"  {label:<20} ret {m['net_return_pct']:+7.1f}%   Sharpe {m['sharpe']:.2f}   "
          f"maxDD {m['max_dd_pct']:5.1f}%   wm {worst_month(m['curve']):+5.1f}%   turn {m['avg_turnover_x']:.2f}{star}")


print("=== SWEEP 1: conviction (weight ~ |score|^k) ===")
for k in (1.0, 1.5, 2.0, 2.5):
    c = copy.deepcopy(base); c.portfolio.conviction = k
    row(f"conviction {k}", run(c), "   <- current" if k == 1.5 else "")

print("\n=== SWEEP 2: sleeve size (longs = shorts) ===")
for n in (3, 4, 5, 6, 7):
    c = copy.deepcopy(base); c.portfolio.longs = n; c.portfolio.shorts = n
    row(f"{n}+{n} names", run(c), "   <- current" if n == 5 else "")

print("\n=== SWEEP 3: rebalance cadence (days) ===")
for d in (1, 2, 3, 5):
    row(f"every {d}d", run(copy.deepcopy(base), rebal=d), "   <- current" if d == 1 else "")
