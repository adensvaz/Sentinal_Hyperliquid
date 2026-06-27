"""Definitive test: partial scale-out — trim X% of a position once it's up Y%, keep the rest running."""
import copy, sys, time as _t
sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import load_history, simulate
base = load_config(); base.signal.momentum_horizons = ["1day","3day","7day","14day"]
fee = 0.00025 if base.execution.maker else 0.00075
slip = 2.0 if base.execution.maker else base.execution.slippage_bps
hist = load_history(base, interval="1day", limit=300)
print(f"daily history: {len(hist['T'])} bars, {len(hist['universe'])} coins\n")
def wm(curve):
    b={}
    for ts,eq,_g in curve:
        ym=_t.strftime("%Y-%m",_t.localtime(ts/1000)); b.setdefault(ym,[eq,eq])[1]=eq
    return min(((v[1]/v[0]-1)*100 for v in b.values()),default=0.0)
def run(pct,frac,label,star=""):
    c=copy.deepcopy(base); c.execution.scale_out_pct=pct; c.execution.scale_out_frac=frac
    m=simulate(hist,c,rebalance_every=1,taker_fee=fee,slippage_bps=slip)
    print(f"  {label:<26} ret {m['net_return_pct']:+7.1f}%   Sharpe {m['sharpe']:.2f}   maxDD {m['max_dd_pct']:5.1f}%   wm {wm(m['curve']):+5.1f}%{star}")
print("=== PARTIAL SCALE-OUT (trim F% once up P%, keep the rest) ===")
run(0,0,"OFF (current)","   <- current")
run(8,0.30,"up 8%  -> trim 30%")
run(8,0.50,"up 8%  -> trim 50%")
run(5,0.30,"up 5%  -> trim 30%")
run(12,0.30,"up 12% -> trim 30%")
run(15,0.50,"up 15% -> trim 50%")
