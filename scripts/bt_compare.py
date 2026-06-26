"""Reusable A/B harness: run the daily backtest under several config mutations on ONE history load.
Usage: ./.venv/bin/python scripts/bt_compare.py <scenario_set>
  beta   - beta-neutral off/on (sanity)
  crash  - momentum-crash guard off/on
  adv    - ADV per-name caps off/on
  final  - baseline vs all-kept-changes
"""
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


def worst_month(curve):
    m = monthly(curve)
    return min(m.values()) if m else 0.0


def neg_months(curve):
    return sum(1 for v in monthly(curve).values() if v < 0)


base = load_config()
base.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
maker = base.execution.maker
fee = 0.00025 if maker else 0.00075
slip = 2.0 if maker else base.execution.slippage_bps


def run(cfg):
    return simulate(load_history.cache, cfg, rebalance_every=1, taker_fee=fee, slippage_bps=slip)


# ---- scenario sets ---------------------------------------------------------
def s_crash():
    off = copy.deepcopy(base); off.risk.crash_guard = False
    on = copy.deepcopy(base); on.risk.crash_guard = True
    return [("crash-guard OFF", off), ("crash-guard ON", on)]


def s_beta():
    off = copy.deepcopy(base); off.portfolio.beta_neutralize = False
    on = copy.deepcopy(base); on.portfolio.beta_neutralize = True
    return [("beta OFF", off), ("beta ON", on)]


def s_adv():
    off = copy.deepcopy(base); off.portfolio.adv_cap_frac = 0.0
    on = copy.deepcopy(base); on.portfolio.adv_cap_frac = getattr(base.portfolio, "adv_cap_frac", 0.10) or 0.10
    return [("adv-cap OFF", off), ("adv-cap ON", on)]


def s_final():
    # cumulative robustness suite OFF vs ON (net-clamp is integrity, on in both; inert in backtest)
    off = copy.deepcopy(base); off.portfolio.beta_neutralize = False; off.risk.crash_guard = False
    on = copy.deepcopy(base); on.portfolio.beta_neutralize = True; on.risk.crash_guard = True
    return [("baseline (no robustness)", off), ("robustness suite ON", on)]


def s_rp():
    off = copy.deepcopy(base); off.portfolio.risk_parity = False
    on = copy.deepcopy(base); on.portfolio.risk_parity = True
    return [("equal-dollar", off), ("risk-parity ON", on)]


def s_disp():
    off = copy.deepcopy(base); off.risk.dispersion_gate = False
    on = copy.deepcopy(base); on.risk.dispersion_gate = True
    return [("dispersion-gate OFF", off), ("dispersion-gate ON", on)]


def s_dailystop():
    off = copy.deepcopy(base); off.risk.daily_loss_limit_pct = 0.0
    on = copy.deepcopy(base); on.risk.daily_loss_limit_pct = 3.0
    return [("daily-stop OFF", off), ("daily-stop ON", on)]


def s_asym():
    off = copy.deepcopy(base); off.execution.asym_rebalance = False
    on = copy.deepcopy(base); on.execution.asym_rebalance = True
    return [("symmetric reb", off), ("asym (winners run)", on)]


def s_resid():
    off = copy.deepcopy(base); off.portfolio.beta_neutral_scores = False
    on = copy.deepcopy(base); on.portfolio.beta_neutral_scores = True
    return [("raw momentum", off), ("residual (beta-aware)", on)]


SETS = {"crash": s_crash, "beta": s_beta, "adv": s_adv, "final": s_final,
        "rp": s_rp, "disp": s_disp, "dailystop": s_dailystop, "asym": s_asym, "resid": s_resid}

which = sys.argv[1] if len(sys.argv) > 1 else "crash"
scenarios = SETS[which]()

load_history.cache = load_history(base, interval="1day", limit=300)
print(f"daily history: {len(load_history.cache['T'])} bars, {len(load_history.cache['universe'])} coins\n")

results = [(name, run(cfg)) for name, cfg in scenarios]

names = [n for n, _ in results]
w = max(18, max(len(n) for n in names) + 2)
hdr = f"{'metric':<22}" + "".join(f"{n:>{w}}" for n in names)
print(hdr); print("-" * len(hdr))


def row(label, fn):
    print(f"{label:<22}" + "".join(f"{fn(m):>{w}}" for _, m in results))


row("net return %", lambda m: f"{m['net_return_pct']:+.1f}")
row("annualized %", lambda m: f"{m['ann_return_pct']:+.0f}")
row("Sharpe", lambda m: f"{m['sharpe']:.2f}")
row("max drawdown %", lambda m: f"{m['max_dd_pct']:.1f}")
row("worst month %", lambda m: f"{worst_month(m['curve']):+.1f}")
row("neg months", lambda m: f"{neg_months(m['curve'])}")
row("hit rate %", lambda m: f"{m['hit_rate_pct']:.0f}")
row("avg turnover x", lambda m: f"{m['avg_turnover_x']:.2f}")
row("mean |book beta|", lambda m: f"{m['book_beta_pre']:.3f}")
