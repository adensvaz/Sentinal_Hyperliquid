"""Robustness check on the 'momentum-weighted + breadth-scaled' combo under the live overlay.
A real edge survives perturbation of its knobs. Vary breadth band, momentum-weight power, and a
per-name weight cap (prudent for a copy-trade lead). If results cluster, it's robust, not curve-fit."""
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
def breadth(i): return sum(1 for c in good if above_ma(c, i, 100))/len(good)

def capw(w, cap):
    if not cap: return w
    w = dict(w)
    for _ in range(8):
        over = [c for c in w if w[c] > cap+1e-9]
        if not over: break
        excess = sum(w[c]-cap for c in over)
        for c in over: w[c] = cap
        under = [c for c in w if w[c] < cap-1e-9]; tu = sum(w[c] for c in under)
        if tu <= 0: break
        for c in under: w[c] += excess*w[c]/tu
    return w

def make(b_lo, b_hi, power, cap):
    def w(i):
        if not above_ma("BTC", i, 100): return {}
        g = min(1.0, max(0.0, (breadth(i)-b_lo)/(b_hi-b_lo)))
        if g <= 0: return {}
        s = [c for c in sorted(good, key=lambda c: mom(c, i), reverse=True)[:K] if mom(c, i) > 0]
        if not s: return {}
        m = {c: max(mom(c, i), 1e-6)**power for c in s}; tot = sum(m.values())
        ww = capw({c: m[c]/tot for c in s}, cap)
        return {c: g*ww[c] for c in s}
    return w

def sig(wf):
    rets, tos, mx = [], [], []; wp = {}
    for i in range(W, n-1):
        w = wf(i)
        tos.append(sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good))
        rets.append(sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0))
        mx.append(max(w.values()) if w else 0.0); wp = w
    return np.array(rets), np.array(tos), np.array(mx)

def overlay(rets, tos):
    eq = START; peak = START; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak
        gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1 + g*rets[t] - g*tos[t]*FEE); peak = max(peak, eq); out.append(eq); rv.append(rets[t])
    return np.array(out)

def stats(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = (eq[-1]/eq[0])**(1/YRS)-1
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    return eq[-1], cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))

print(f"Robustness grid — momentum-weighted + breadth-scaled, through live overlay ({YRS:.1f}yr)\n")
print(f"{'breadth band':<14}{'pow':>4}{'cap':>6}{'final$':>10}{'CAGR':>6}{'Shrp':>6}{'maxDD':>7}{'wHalf':>7}{'maxW':>6}")
print("-"*66)
res = []
for (lo, hi) in [(0.20, 0.70), (0.30, 0.80), (0.35, 0.85), (0.40, 0.90)]:
    for power in [0.5, 1.0]:
        for cap in [0.0, 0.30]:
            rets, tos, mx = sig(make(lo, hi, power, cap))
            fin, c, s, d, wh = stats(overlay(rets, tos))
            res.append((s, d, wh))
            capn = f"{cap:.2f}" if cap else "none"
            print(f"{lo:.2f}-{hi:.2f}     {power:>4.1f}{capn:>6}{fin:>9,.0f}{c*100:>5.0f}%{s:>6.2f}{d*100:>6.0f}%{wh:>7.2f}{mx.max():>6.2f}")
print("-"*66)
S = np.array(res)
print(f"\nclustering across {len(res)} settings:  Sharpe {S[:,0].min():.2f}-{S[:,0].max():.2f}  "
      f"maxDD {S[:,1].min()*100:.0f}-{S[:,1].max()*100:.0f}%  worstHalf {S[:,2].min():.2f}-{S[:,2].max():.2f}")
print("baseline reference (equal-wt, no breadth):  Sharpe 1.25  maxDD 35%  worstHalf 1.10")
print("maxW = largest single-name weight ever taken (concentration check for a copy-trade lead).")
