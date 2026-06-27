"""Out-of-sample robustness: run the CURRENT strategy on independent sub-periods of the history.
If the edge only shows in one window, it's fragile/overfit. If it holds across halves & quarters, it's real."""
import copy, sys, time as _t
sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import load_history, simulate

base = load_config(); base.signal.momentum_horizons = ["1day","3day","7day","14day"]
fee = 0.00025 if base.execution.maker else 0.00075
slip = 2.0 if base.execution.maker else base.execution.slippage_bps
H = load_history(base, interval="1day", limit=300)
T, closes, vols, uni = H["T"], H["closes"], H["vols"], H["universe"]
n = len(T)
print(f"daily history: {n} bars, {len(uni)} coins\n")

def slice_hist(a, b):
    return {"universe": uni, "T": T[a:b], "interval_h": H["interval_h"],
            "closes": {c: closes[c][a:b] for c in uni}, "vols": {c: vols[c][a:b] for c in uni}}

def run(a, b, label):
    m = simulate(slice_hist(a, b), copy.deepcopy(base), rebalance_every=1, taker_fee=fee, slippage_bps=slip)
    if "error" in m: print(f"  {label:<18} {m['error']}"); return
    d0=_t.strftime("%b%d",_t.localtime(T[a]/1000)); d1=_t.strftime("%b%d",_t.localtime(T[min(b,n)-1]/1000))
    print(f"  {label:<18} {d0}->{d1}  ret {m['net_return_pct']:+7.1f}%   Sharpe {m['sharpe']:5.2f}   maxDD {m['max_dd_pct']:5.1f}%   hit {m['hit_rate_pct']:.0f}%")

print("=== FULL WINDOW ==="); run(0, n, "full")
print("\n=== SPLIT-HALF (independent periods) ==="); run(0, n//2, "1st half"); run(n//2, n, "2nd half")
print("\n=== QUARTERS ===")
q=n//4
for i in range(4): run(i*q, (i+1)*q if i<3 else n, f"Q{i+1}")
