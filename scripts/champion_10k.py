"""$10K in Jan-2021 -> now with the champion. Two honest numbers:
  RAW    = full 1x gross (idealized signal).
  LIVE   = the balanced overlay you actually run (vol-target ~1.8%/day + drawdown throttle, gross 0.25-1x).
5.5yr survivorship-free daily Binance data. Config = top5 lb30 reg100 dual-OFF, equal-weight long-only."""
import sys, httpx, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
def kl(s, end=None):
    p = {"symbol": s+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=20).json()
def f(c):
    p1 = kl(c)
    if not isinstance(p1, list) or not p1: return None
    p2 = kl(c, end=int(p1[0][0])-1)
    return c, {int(r[0]): float(r[4]) for r in (p2 if isinstance(p2, list) else [])+p1}
ser = {c: d for c, d in (r for r in pmap(f, COINS, workers=10) if r)}
ref = max(ser, key=lambda c: len(ser[c])); T = sorted(ser[ref]); P = {}; good = []
for c in ser:
    a, have, lc = [], 0, None
    for t in T:
        if t in ser[c]: lc = ser[c][t]; have += 1
        a.append(lc)
    if have/len(T) >= .9 and a[0] is not None: P[c] = np.array(a, float); good.append(c)
n = len(T); FEE = 0.0004; W = 110; LB, K, REGM = 30, 5, 100; START = 10_000.0
def reg(i, m): return P["BTC"][i] > P["BTC"][i-m:i].mean()
def wfn(i):
    if not reg(i, REGM): return {}
    s = sorted(good, key=lambda c: P[c][i]/P[c][i-LB]-1, reverse=True)[:K]
    return {c: 1.0/K for c in s} if s else {}

# 1) raw per-day book return + turnover at unit gross
rets, tos = [], []; wp = {}
for i in range(W, n-1):
    w = wfn(i)
    tos.append(sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good))
    rets.append(sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)); wp = w
rets, tos = np.array(rets), np.array(tos)

def walk(scale_fn):
    eq = START; peak = START; out = [eq]; rv = []
    for t in range(len(rets)):
        g = scale_fn(t, eq, peak, rv)
        eq *= (1 + g*rets[t] - g*tos[t]*FEE)
        peak = max(peak, eq); out.append(eq)
        rv.append(rets[t])
    return np.array(out)

raw = walk(lambda t, eq, peak, rv: 1.0)
# balanced overlay: vol-target 1.8%/day on trailing-21 raw vol + DD throttle 0.12->0.35 to floor 0.25
def bal_scale(t, eq, peak, rv):
    vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
    gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
    dd = 1 - eq/peak
    if dd <= 0.12: gd = 1.0
    elif dd >= 0.35: gd = 0.25
    else: gd = 1.0 - (dd-0.12)/(0.35-0.12)*(1.0-0.25)
    return max(0.25, min(1.0, gv, gd))
bal = walk(bal_scale)

d0 = datetime.fromtimestamp(T[W]/1000, tz=timezone.utc).date()
d1 = datetime.fromtimestamp(T[-1]/1000, tz=timezone.utc).date()
yrs = len(rets)/365
def cagr(e): return (e[-1]/e[0])**(1/yrs)-1
def mdd(e): return ((np.maximum.accumulate(e)-e)/np.maximum.accumulate(e)).max()
print(f"$10,000 in champion · {d0} -> {d1} ({yrs:.1f} yr)\n")
print(f"{'scenario':<34}{'final $':>14}{'multiple':>10}{'CAGR':>7}{'maxDD':>7}")
print("-"*72)
print(f"{'RAW  (full 1x gross, idealized)':<34}{raw[-1]:>13,.0f} {raw[-1]/START:>8.1f}x{cagr(raw)*100:>6.0f}%{mdd(raw)*100:>6.0f}%")
print(f"{'LIVE (vol-target + DD-throttle)':<34}{bal[-1]:>13,.0f} {bal[-1]/START:>8.1f}x{cagr(bal)*100:>6.0f}%{mdd(bal)*100:>6.0f}%")
print("-"*72)
print("\nReality is BELOW the LIVE line: this assumes ideal maker fills, no slippage, excludes funding,")
print("and the 26-coin set all survived (survivorship). Treat LIVE as an optimistic ceiling, not a promise.")
