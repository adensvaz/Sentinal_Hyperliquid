"""Champion 'less boring' study: which higher-ACTIVITY variants keep the edge?

Baseline champion = D top5 lb30 reg100 daily, 100% CASH when BTC < 100d MA.
The user finds it boring (sits in cash ~half the time, holds only 5 names).
This sweeps the activity levers (more names / faster lookback / faster regime /
no-dual / hold-majors-in-riskoff / soft-regime / long-short-in-riskoff / no-regime)
and reports the EDGE metrics AND the ACTIVITY metrics side-by-side, so we can see
exactly what each lever costs in Sharpe and buys in screen-time.

Data: 5.5yr daily Binance closes, 28-coin universe (same as sweep_champion.py).
"""
import sys, httpx, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap

COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
MAJORS = ["BTC","ETH"]

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
n = len(T); FEE = 0.0004; W = 110; YRS = n/365.0

def met(eq):
    eq = np.array(eq); r = eq[1:]/eq[:-1]-1
    cagr = ((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    sh = r.mean()/r.std()*np.sqrt(365) if r.std() > 0 else 0
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh, dd

def run(wfn, a, b, every=1):
    """Return equity curve + activity stats (inv% bars in market, avg #names, annual turnover)."""
    eq = [1.0]; wp = {}; invbars = 0; npos_sum = 0; to_sum = 0.0; bars = 0
    for i in range(a+W, b-1):
        if (i-(a+W)) % every == 0: w = wfn(i)
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good)
        ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)
        eq.append(eq[-1]*(1+ret-to*FEE)); wp = w
        bars += 1; to_sum += to
        live = [c for c in good if abs(w.get(c, 0)) > 1e-9]
        if live: invbars += 1
        npos_sum += len(live)
    inv = 100*invbars/bars; avgpos = npos_sum/bars; turn_yr = to_sum/YRS
    return eq, (inv, avgpos, turn_yr)

def score(wfn, every=1):
    eqf, act = run(wfn, 0, n, every); fl = met(eqf)
    h1 = met(run(wfn, 0, n//2, every)[0]); h2 = met(run(wfn, n//2, n, every)[0])
    return fl, min(h1[1], h2[1]), act

def reg(i, m): return P["BTC"][i] > P["BTC"][i-m:i].mean()

def ranked(i, lb, dual):
    s = sorted(good, key=lambda c: P[c][i]/P[c][i-lb]-1, reverse=True)
    if dual: s = [c for c in s if P[c][i] > P[c][i-50:i].mean()]
    return s

# ---- weight functions: baseline + activity variants ----
def champ(k, lb, regm, dual):              # baseline: cash in risk-off
    def w(i):
        if not reg(i, regm): return {}
        s = ranked(i, lb, dual)[:k]
        return {c: 1.0/k for c in s} if s else {}
    return w

def majors_off(k, lb, regm, dual):         # risk-off: hold BTC/ETH instead of cash
    def w(i):
        if not reg(i, regm):
            mj = [c for c in MAJORS if c in good]
            return {c: 1.0/len(mj) for c in mj} if mj else {}
        s = ranked(i, lb, dual)[:k]
        return {c: 1.0/k for c in s} if s else {}
    return w

def soft_off(k, lb, regm, dual, floor):    # risk-off: stay floor-invested in same top-K
    def w(i):
        s = ranked(i, lb, dual)[:k]
        if not s: return {}
        g = 1.0 if reg(i, regm) else floor
        return {c: g/k for c in s}
    return w

def ls_off(k, lb, regm, dual):             # risk-off: long top-k / short bottom-k (market-neutral)
    def w(i):
        s = ranked(i, lb, False)
        if reg(i, regm):
            top = ranked(i, lb, dual)[:k]
            return {c: 1.0/k for c in top} if top else {}
        lo, hi = s[-k:], s[:k]
        d = {}
        for c in hi: d[c] = 0.5/k
        for c in lo: d[c] = -0.5/k
        return d
    return w

def noregime(k, lb, dual):                 # pure momentum, never cash (shows why regime matters)
    def w(i):
        s = ranked(i, lb, dual)[:k]
        return {c: 1.0/k for c in s} if s else {}
    return w

def bh():                                  # buy & hold BTC benchmark
    return lambda i: {"BTC": 1.0}

VARIANTS = [
    ("CURRENT LIVE  D top5 lb30 reg100",        champ(5, 30, 100, True)),
    ("-- decision frontier: no-dual x top_k (lb30) --", None),
    ("no-dual top5  C top5 lb30 reg100",        champ(5, 30, 100, False)),
    ("no-dual top6  C top6 lb30 reg100",        champ(6, 30, 100, False)),
    ("no-dual top7  C top7 lb30 reg100",        champ(7, 30, 100, False)),
    ("no-dual top8  C top8 lb30 reg100",        champ(8, 30, 100, False)),
]

print(f"{len(good)} coins, {n} bars, {YRS:.1f}yr, fee {FEE*1e4:.0f}bps/turn\n")
print(f"{'variant':<42}{'CAGR':>6}{'Shrp':>6}{'maxDD':>7}{'wHalf':>7} | {'inv%':>5}{'#pos':>6}{'turn/yr':>8}")
print("-"*94)
base_sh = None
for lab, wfn in VARIANTS:
    if wfn is None:
        print(f"\n{lab}")
        continue
    fl, wh, act = score(wfn)
    inv, avgpos, turn = act
    if base_sh is None: base_sh = fl[1]
    print(f"{lab:<42}{fl[0]:>+5.0f}%{fl[1]:>6.2f}{fl[2]:>6.0f}%{wh:>7.2f} | {inv:>4.0f}%{avgpos:>6.1f}{turn:>7.1f}x")
print("-"*94)
print("inv% = bars holding ANY position (screen-time) · #pos = avg names held · turn/yr = annual round-trip turnover")
print("wHalf = worst of the two 2.75yr halves' Sharpe (out-of-sample robustness). Higher = more durable.")
