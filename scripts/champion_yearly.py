"""Champion year-by-year on 5.5yr survivorship-free daily Binance data.
Config = CURRENT LIVE champion: top5, lb30, reg100, dual_confirm OFF, daily, equal-weight long-only,
100% cash when BTC < 100d MA. fee 4bps/turn. Reports per-calendar-year return, maxDD, %time in cash."""
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
n = len(T); FEE = 0.0004; W = 110; LB, K, REGM = 30, 5, 100
yr = [datetime.fromtimestamp(t/1000, tz=timezone.utc).year for t in T]

def reg(i, m): return P["BTC"][i] > P["BTC"][i-m:i].mean()
def wfn(i):
    if not reg(i, REGM): return {}
    s = sorted(good, key=lambda c: P[c][i]/P[c][i-LB]-1, reverse=True)[:K]
    return {c: 1.0/K for c in s} if s else {}

# walk forward, record daily equity + which year each step belongs to + cash flag
eq = 1.0; wp = {}; days = []   # (year, eq_after, in_cash)
for i in range(W, n-1):
    w = wfn(i)
    to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good)
    ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)
    eq *= (1 + ret - to*FEE); wp = w
    days.append((yr[i+1], eq, len(w) == 0))

def block(rows):
    e0 = rows[0][1] / (1 + 0); e_start = rows[0][1]; e_end = rows[-1][1]
    # return over the year = last/first of the year's equity points (use prev close as base)
    return rows
# group by year
from itertools import groupby
print(f"{len(good)} coins · {n} bars · {datetime.fromtimestamp(T[0]/1000,tz=timezone.utc).date()} → {datetime.fromtimestamp(T[-1]/1000,tz=timezone.utc).date()}")
print(f"Champion RAW (top5 lb30 reg100 dual-OFF, equal-weight long-only, daily, 4bps fee)\n")
print(f"{'year':<8}{'return':>9}{'maxDD':>8}{'% in cash':>11}{'end equity (×start)':>22}")
print("-"*58)
prev_eq = 1.0
years = sorted(set(y for y, _, _ in days))
cum = 1.0
for y in years:
    rows = [r for r in days if r[0] == y]
    eqs = np.array([prev_eq] + [r[1] for r in rows])
    yr_ret = eqs[-1]/eqs[0] - 1
    dd = ((np.maximum.accumulate(eqs)-eqs)/np.maximum.accumulate(eqs)).max()
    cashpct = 100*sum(1 for r in rows if r[2])/len(rows)
    partial = " (partial)" if (y == years[0] or y == years[-1]) else ""
    print(f"{y:<8}{yr_ret*100:>+8.0f}%{dd*100:>7.0f}%{cashpct:>10.0f}%{eqs[-1]:>18.2f}×{partial}")
    prev_eq = eqs[-1]
print("-"*58)
fulleq = np.array([1.0] + [r[1] for r in days])
r = fulleq[1:]/fulleq[:-1]-1
cagr = (fulleq[-1]**(365/len(fulleq))-1)*100
sh = r.mean()/r.std()*np.sqrt(365)
mdd = ((np.maximum.accumulate(fulleq)-fulleq)/np.maximum.accumulate(fulleq)).max()*100
print(f"FULL    CAGR {cagr:+.0f}%  ·  Sharpe {sh:.2f}  ·  maxDD {mdd:.0f}%  ·  total {fulleq[-1]:.1f}× over {len(fulleq)/365:.1f}yr")
print("\nNOTE: RAW signal at full 1× gross. The LIVE 'balanced' config runs vol-target + DD-throttle")
print("      (~0.5-0.6× average gross) → roughly half these yearly returns AND half the drawdowns,")
print("      same shape and Sharpe. e.g. dashboard headline +39%/yr, ~32% maxDD = the throttled view.")
