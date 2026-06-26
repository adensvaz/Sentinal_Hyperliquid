"""Test take-profit on winners: close a held name once its gain since entry exceeds X%."""
import copy, sys, time as _t
sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import load_history, simulate

base = load_config()
base.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
fee = 0.00025 if base.execution.maker else 0.00075
slip = 2.0 if base.execution.maker else base.execution.slippage_bps
hist = load_history(base, interval="1day", limit=300)
print(f"daily history: {len(hist['T'])} bars, {len(hist['universe'])} coins\n")

def wm(curve):
    b={}
    for ts,eq,_g in curve:
        ym=_t.strftime("%Y-%m",_t.localtime(ts/1000)); b.setdefault(ym,[eq,eq])[1]=eq
    return min(((v[1]/v[0]-1)*100 for v in b.values()),default=0.0)

def run(tp):
    c=copy.deepcopy(base); c.execution.take_profit_pct=tp
    m=simulate(hist,c,rebalance_every=1,taker_fee=fee,slippage_bps=slip)
    star="   <- current (off)" if tp==0 else ""
    print(f"  TP {('off' if tp==0 else str(tp)+'%'):<6} ret {m['net_return_pct']:+7.1f}%   Sharpe {m['sharpe']:.2f}   maxDD {m['max_dd_pct']:5.1f}%   wm {wm(m['curve']):+5.1f}%   hit {m['hit_rate_pct']:.0f}%{star}")

print("=== TAKE-PROFIT on winners (close once gain since entry exceeds X%) ===")
for tp in (0, 5, 8, 12, 20, 30):
    run(tp)
