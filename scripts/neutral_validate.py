"""Heavy-validate the lead from neutral_research.py: dollar-neutral cross-sectional MOMENTUM,
rebalanced WEEKLY (vs the current DAILY). Don't repeat the breadth mistake — stress it on fees,
universe bootstrap, and report the drawdown honestly before recommending."""
import sys, httpx, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(5)
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
n = len(T); W = 70; YRS = n/365.0
def mom(c, i, lb): return P[c][i]/P[c][i-lb]-1 if P[c][i-lb] > 0 else -9
def met(eq):
    eq = np.array(eq); r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = ((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))
def run(coins, lb, ev, K, fee):
    eq = [1.0]; wp = {}
    for i in range(W, n-1):
        if (i-W) % ev == 0:
            rank = sorted(coins, key=lambda c: mom(c, i, lb), reverse=True)
            w = {c: 1.0/K for c in rank[:K]}
            for c in rank[-K:]: w[c] = w.get(c, 0) - 1.0/K
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in coins)
        ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in coins if P[c][i] > 0)
        eq.append(eq[-1]*(1+ret-to*fee)); wp = w
    return eq

print(f"{len(good)} coins · {YRS:.1f}yr · K=5/side · dollar-neutral cross-sectional MOMENTUM\n")
print("[A] PARAMETER NEIGHBORHOOD (weekly) — is the lb14/rebal7d cell isolated or part of a plateau?")
hdr = "lb v rebal(d)"
print(f"    {hdr:<14}{'5d':>8}{'6d':>8}{'7d':>8}{'8d':>8}{'10d':>8}  (Sharpe)")
for lb in (10, 14, 20, 30):
    row = ""
    for ev in (5, 6, 7, 8, 10):
        _, sh, _, _ = met(run(good, lb, ev, 5, 0.0004))
        row += f"{sh:>8.2f}"
    print(f"    lb{lb:<12}{row}")

print("\n[B] FEE SENSITIVITY (lb14 rebal7d) — turnover ~114x/yr, does it survive real costs?")
for bps in (4, 8, 15, 25, 40):
    c, sh, dd, wh = met(run(good, 14, 7, 5, bps/1e4))
    print(f"    {bps:>2}bps:  CAGR {c:>+4.0f}%  Sharpe {sh:.2f}  maxDD {dd:.0f}%  wHalf {wh:.2f}")

print("\n[C] UNIVERSE BOOTSTRAP (lb14 rebal7d, 8bps) — 120 random 18-coin subsets")
shs, dds = [], []
for _ in range(120):
    sub = list(rng.choice(good, size=18, replace=False))
    c, sh, dd, wh = met(run(sub, 14, 7, 5, 0.0008))
    shs.append(sh); dds.append(dd)
shs, dds = np.array(shs), np.array(dds)
print(f"    Sharpe: mean {shs.mean():.2f} · >0.5 in {100*(shs>0.5).mean():.0f}% · >0 in {100*(shs>0).mean():.0f}% · [p5 {np.percentile(shs,5):.2f}, p95 {np.percentile(shs,95):.2f}]")
print(f"    maxDD : mean {dds.mean():.0f}% · [p5 {np.percentile(dds,5):.0f}%, p95 {np.percentile(dds,95):.0f}%]  <- honest drawdown reality")

print("\n[D] HEAD-TO-HEAD: current cadence vs weekly (lb14, 8bps, full universe)")
for ev, lab in ((1, "DAILY (current)"), (7, "WEEKLY (proposed)")):
    c, sh, dd, wh = met(run(good, 14, ev, 5, 0.0008))
    print(f"    {lab:<18} CAGR {c:>+4.0f}%  Sharpe {sh:.2f}  maxDD {dd:.0f}%  wHalf {wh:.2f}")
print("-"*70)
