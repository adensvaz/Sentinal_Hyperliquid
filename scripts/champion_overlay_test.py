"""Decisive test: do the two promising rules still help AFTER the live vol-target+DD-throttle overlay?
Candidates from champion_research.py:
  (a) momentum-weighted sizing  -> best raw Sharpe (+0.08) but +7pt DD, -0.08 robustness
  (b) breadth-scaled gross      -> best raw DD cut (60->44%) at ~flat robustness (the 'breadth check')
The overlay already cuts gross in bad times, so these may be REDUNDANT. Measure $10k->now, CAGR, Sharpe, maxDD.
"""
import sys, httpx, numpy as np
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
n = len(T); FEE = 0.0004; W = 110; YRS = n/365.0; K = 5; START = 10_000.0
def mom(c, i, lb=30): return P[c][i]/P[c][i-lb]-1 if P[c][i-lb] > 0 else -1e9
def above_ma(c, i, m): return P[c][i] > P[c][i-m:i].mean()

def w_baseline(i):
    if not above_ma("BTC", i, 100): return {}
    s = [c for c in sorted(good, key=lambda c: mom(c, i), reverse=True)[:K] if mom(c, i) > 0]
    return {c: 1.0/len(s) for c in s} if s else {}
def w_momwt(i):
    if not above_ma("BTC", i, 100): return {}
    s = [c for c in sorted(good, key=lambda c: mom(c, i), reverse=True)[:K] if mom(c, i) > 0]
    if not s: return {}
    m = {c: max(mom(c, i), 1e-6) for c in s}; tot = sum(m.values())
    return {c: m[c]/tot for c in s}
def breadth(i): return sum(1 for c in good if above_ma(c, i, 100))/len(good)
def w_breadth(i):
    if not above_ma("BTC", i, 100): return {}
    g = min(1.0, max(0.0, (breadth(i)-0.3)/0.5))
    if g <= 0: return {}
    s = [c for c in sorted(good, key=lambda c: mom(c, i), reverse=True)[:K] if mom(c, i) > 0]
    return {c: g/len(s) for c in s} if s else {}
def w_both(i):
    if not above_ma("BTC", i, 100): return {}
    g = min(1.0, max(0.0, (breadth(i)-0.3)/0.5))
    if g <= 0: return {}
    s = [c for c in sorted(good, key=lambda c: mom(c, i), reverse=True)[:K] if mom(c, i) > 0]
    if not s: return {}
    m = {c: max(mom(c, i), 1e-6) for c in s}; tot = sum(m.values())
    return {c: g*m[c]/tot for c in s}

def sig_series(wf):
    rets, tos = [], []; wp = {}
    for i in range(W, n-1):
        w = wf(i)
        tos.append(sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good))
        rets.append(sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)); wp = w
    return np.array(rets), np.array(tos)

def overlay(rets, tos, on=True):
    eq = START; peak = START; out = [eq]; rv = []
    for t in range(len(rets)):
        if on:
            vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
            gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
            dd = 1-eq/peak
            gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
            g = max(0.25, min(1.0, gv, gd))
        else:
            g = 1.0
        eq *= (1 + g*rets[t] - g*tos[t]*FEE); peak = max(peak, eq); out.append(eq); rv.append(rets[t])
    return np.array(out)

def stats(eq):
    r = eq[1:]/eq[:-1]-1
    cagr = (eq[-1]/eq[0])**(1/YRS)-1
    sh = r.mean()/r.std()*np.sqrt(365)
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    # worst-half sharpe
    h = len(r)//2
    def shp(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    wh = min(shp(r[:h]), shp(r[h:]))
    return eq[-1], cagr, sh, dd, wh

VAR = [("baseline (equal-weight)", w_baseline), ("momentum-weighted", w_momwt),
       ("breadth-scaled", w_breadth), ("both", w_both)]
print(f"$10k -> now under the LIVE overlay (vol-target + DD-throttle). {YRS:.1f}yr, {len(good)} coins.\n")
print(f"{'variant':<24}{'final $':>12}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'wHalf':>7}")
print("-"*65)
for lab, wf in VAR:
    rets, tos = sig_series(wf)
    f, c, s, d, wh = stats(overlay(rets, tos, on=True))
    print(f"{lab:<24}{f:>11,.0f} {c*100:>5.0f}%{s:>8.2f}{d*100:>6.0f}%{wh:>7.2f}")
print("-"*65)
print("(All run THROUGH the live overlay — this is the deployed-strategy comparison, not the raw signal.)")
