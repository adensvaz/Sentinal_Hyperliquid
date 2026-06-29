"""Decompose the change under the FAITHFUL overlay (realized-return vol-target, matching
risk.limits.risk_scale which feeds on store.daily_returns). Isolate breadth vs momentum-weighting
vs both, full metrics + universe bootstrap robustness vs the ORIGINAL champion (OLD)."""
import sys, httpx, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(11)
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
n = len(T); W = 110; YRS = n/365.0
MOM, ABV, FWD = {}, {}, {}
for c in good:
    m = np.full(n, -9.0); ab = np.zeros(n, bool); fw = np.zeros(n)
    for i in range(W, n-1):
        if P[c][i-30] > 0: m[i] = P[c][i]/P[c][i-30]-1
        ab[i] = P[c][i] > P[c][i-100:i].mean(); fw[i] = P[c][i+1]/P[c][i]-1
    MOM[c], ABV[c], FWD[c] = m, ab, fw
REG = np.array([P["BTC"][i] > P["BTC"][i-100:i].mean() if i >= 100 else False for i in range(n)])

def capw(st, cap):
    pos = {s: max(v, 1e-9) for s, v in st.items()}; tot = sum(pos.values()) or 1.0
    w = {s: v/tot for s, v in pos.items()}
    if not cap or len(w)*cap < 1.0: return w
    for _ in range(8):
        over = [s for s in w if w[s] > cap+1e-12]
        if not over: break
        exc = sum(w[s]-cap for s in over)
        for s in over: w[s] = cap
        und = [s for s in w if w[s] < cap-1e-12]; base = sum(w[s] for s in und)
        if base <= 0: break
        for s in und: w[s] += exc*w[s]/base
    return w
def signal(coins, momwt, breadth, K=5):
    rets, tos = [], []; wp = {}
    for i in range(W, n-1):
        if not REG[i]: w = {}
        else:
            cand = sorted(((c, MOM[c][i]) for c in coins if MOM[c][i] > 0), key=lambda x: x[1], reverse=True)[:K]
            if not cand: w = {}
            else:
                g = 1.0
                if breadth:
                    frac = np.mean([ABV[c][i] for c in coins]); g = min(1.0, max(0.0, (frac-0.30)/0.50))
                if g <= 0: w = {}
                else:
                    ww = capw({c: m for c, m in cand}, 0.40) if momwt else {c: 1.0/len(cand) for c, m in cand}
                    w = {c: g*ww[c] for c in ww}
        tos.append(sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in coins)); rets.append(sum(w.get(c, 0)*FWD[c][i] for c in coins)); wp = w
    return np.array(rets), np.array(tos)
def overlay(rets, tos, fee=0.0004):   # FAITHFUL: vol-target on REALIZED (scaled) returns
    eq = 1.0; peak = 1.0; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak; gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1 + g*rets[t] - g*tos[t]*fee); peak = max(peak, eq); out.append(eq); rv.append(g*rets[t])
    return np.array(out)
def metr(coins, momwt, breadth):
    eq = overlay(*signal(coins, momwt, breadth)); r = eq[1:]/eq[:-1]-1
    sh = r.mean()/r.std()*np.sqrt(365); d = r[r < 0]
    so = r.mean()/(d.std() if len(d) > 1 else 1e-9)*np.sqrt(365)
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    cagr = eq[-1]**(1/YRS)-1
    return cagr, sh, so, dd, (cagr/dd if dd > 0 else 0)

VAR = [("OLD  equal-wt, no breadth", False, False), ("B    + breadth only", False, True),
       ("M    + mom-weight only", True, False), ("BM   + both (shipped)", True, True)]
print(f"FAITHFUL overlay · {len(good)} coins · {YRS:.1f}yr\n")
print(f"{'variant':<28}{'CAGR%':>7}{'Sharpe':>8}{'Sortino':>9}{'maxDD%':>8}{'Calmar':>8}")
print("-"*68)
for lab, mw, br in VAR:
    c, sh, so, dd, ca = metr(good, mw, br)
    print(f"{lab:<28}{c*100:>6.0f}%{sh:>8.2f}{so:>9.2f}{dd*100:>7.0f}%{ca:>8.2f}")
print("-"*68)

print("\nUNIVERSE BOOTSTRAP vs OLD (120 random 18-coin subsets): ΔSharpe / Δsortino / ΔmaxDD, NEW-win%")
def boot(mw, br, K=120):
    ds, dso, dd = [], [], []
    for _ in range(K):
        sub = list(rng.choice(good, size=18, replace=False))
        if "BTC" not in sub: sub[0] = "BTC"
        co, sho, soo, ddo, _ = metr(sub, False, False)
        cn, shn, son, ddn, _ = metr(sub, mw, br)
        ds.append(shn-sho); dso.append(son-soo); dd.append((ddn-ddo)*100)
    return np.array(ds), np.array(dso), np.array(dd)
for lab, mw, br in VAR[1:]:
    ds, dso, dd = boot(mw, br)
    print(f"  {lab:<26} ΔSh {ds.mean():+.2f} (win {100*(ds>0).mean():.0f}%) · "
          f"ΔSo {dso.mean():+.2f} · ΔDD {dd.mean():+.1f}pt (lower {100*(dd<0).mean():.0f}%)")
print("\nRead: positive ΔSharpe win% >50 means it robustly helps risk-adjusted return; ΔDD lower% means safer.")
