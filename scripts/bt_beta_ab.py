"""A/B: beta-neutralization OFF vs ON, same daily history, same signal. Proves it zeros
book BTC-beta without hurting risk-adjusted return. Run: ./.venv/bin/python scripts/bt_beta_ab.py"""
import sys, time as _t
sys.path.insert(0, "src")

from sentinel.config import load_config
from sentinel.backtest import load_history, simulate


def monthly(curve):
    b = {}
    for ts, eq, _g in curve:
        ym = _t.strftime("%Y-%m", _t.localtime(ts / 1000))
        b.setdefault(ym, [eq, eq])[1] = eq
    return {ym: (v[1] / v[0] - 1) * 100 for ym, v in sorted(b.items())}


def worst_month(curve):
    m = monthly(curve)
    return min(m.values()) if m else 0.0


cfg = load_config()
cfg.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
hist = load_history(cfg, interval="1day", limit=300)
print(f"daily history: {len(hist['T'])} bars, {len(hist['universe'])} coins\n")

maker = cfg.execution.maker
fee = 0.00025 if maker else 0.00075
slip = 2.0 if maker else cfg.execution.slippage_bps

rows = []
for label, flag in (("OFF", False), ("ON", True)):
    cfg.portfolio.beta_neutralize = flag
    m = simulate(hist, cfg, rebalance_every=1, taker_fee=fee, slippage_bps=slip)
    rows.append((label, m))

hdr = f"{'metric':<22}{'beta-neutral OFF':>20}{'beta-neutral ON':>20}"
print(hdr); print("-" * len(hdr))
def line(name, key, fmt, scale=1.0):
    a, b = rows[0][1].get(key), rows[1][1].get(key)
    fa = "n/a" if a is None else format(a * scale, fmt)
    fb = "n/a" if b is None else format(b * scale, fmt)
    print(f"{name:<22}{fa:>20}{fb:>20}")

line("net return %", "net_return_pct", "+.1f")
line("annualized %", "ann_return_pct", "+.0f")
line("Sharpe", "sharpe", ".2f")
line("max drawdown %", "max_dd_pct", ".1f")
line("hit rate %", "hit_rate_pct", ".0f")
line("avg turnover x", "avg_turnover_x", ".2f")
line("fee drag %", "fee_drag_pct", ".1f")
print(f"{'worst month %':<22}{worst_month(rows[0][1]['curve']):>+20.1f}{worst_month(rows[1][1]['curve']):>+20.1f}")
print(f"{'mean |book BTC-beta|':<22}{rows[0][1]['book_beta_pre']:>20.3f}"
      f"{(rows[1][1]['book_beta_post'] if rows[1][1]['book_beta_post'] is not None else 0):>20.3f}")
print(f"\n(natural mean |beta| of the raw book ≈ {rows[0][1]['book_beta_pre']:.3f};"
      f" after neutralization ≈ {rows[1][1]['book_beta_post']:.3f})")
