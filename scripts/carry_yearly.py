"""Carry strategy year-by-year performance — production config (trailing-avg funding, 8bps, live overlay).
$10k start. Reports: annual return, maxDD that year, Sharpe, % time with open positions."""
import sys, httpx, time as _t, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(4)
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ",
         "TRX","FTM","MANA","AXS","EGLD","CHZ","ENJ","DASH","WAVES","COMP","SUSHI","GALA"]
DAY = 86400000
def klines(c, end=None):
    p = {"symbol": c+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    try: return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=25).json()
    except Exception: return []
def price_hist(c):
    out, end = {}, None
    for _ in range(5):
        rows = klines(c, end)
        if not isinstance(rows, list) or not rows: break
        for r in rows: out[int(r[0])//DAY] = float(r[4])
        end = int(rows[0][0])-1
        if len(rows) < 1000: break
    return (c, out) if len(out) > 200 else None
def fund_hist(c):
    out, start = {}, 1530000000000
    for _ in range(40):
        p = {"symbol": c+"USDT", "limit": 1000, "startTime": start}
        try: rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params=p, timeout=25).json()
        except Exception: break
        if not isinstance(rows, list) or not rows: break
        for x in rows:
            out.setdefault(int(x["fundingTime"])//DAY, 0.0)
            out[int(x["fundingTime"])//DAY] += float(x["fundingRate"])
        if len(rows) < 1000: break
        start = int(rows[-1]["fundingTime"])+1; _t.sleep(0.12)
    return (c, out) if len(out) > 150 else None
print("fetching data…")
PX = {c: d for c, d in (r for r in pmap(price_hist, COINS, workers=10) if r)}
FD = {c: d for c, d in (r for r in pmap(fund_hist, [c for c in COINS if c in PX], workers=4) if r)}
coins = [c for c in PX if c in FD]
days = sorted(set().union(*[set(PX[c]) for c in coins]))
P = {c: np.array([PX[c].get(d, np.nan) for d in days], float) for c in coins}
F = {c: np.array([FD[c].get(d, np.nan) for d in days], float) for c in coins}
av = {c: (~np.isnan(P[c])) & (P[c] > 0) & (~np.isnan(F[c])) for c in coins}
ncoin = np.array([sum(av[c][i] for c in coins) for i in range(len(days))])
start = int(np.argmax(ncoin >= 8))
days = days[start:]; n = len(days)
for c in coins: P[c], F[c], av[c] = P[c][start:], F[c][start:], av[c][start:]
YRS = n/365.0
YEAR = np.array([datetime.fromtimestamp(d*DAY/1000, tz=timezone.utc).year for d in days])
START_USD = 10_000.0

def avgF(c, i, look=5):
    a = F[c][max(0, i-look+1):i+1]; a = a[~np.isnan(a)]
    return a.mean() if len(a) else np.nan

def signal_returns(fee=0.0008):
    rets, tos, inv = [], [], []; wp = {}
    for i in range(21, n-1):
        elig = [c for c in coins if av[c][i] and av[c][i+1] and not np.isnan(avgF(c, i))]
        if len(elig) >= 12:
            rank = sorted(elig, key=lambda c: avgF(c, i))
            w = {c: 0.2 for c in rank[:5]}
            for c in rank[-5:]: w[c] = w.get(c, 0) - 0.2
        else: w = {}
        tos.append(sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in coins))
        pr = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in coins if w.get(c) and av[c][i] and av[c][i+1])
        fn = sum(-w.get(c, 0)*F[c][i+1] for c in coins if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); inv.append(1 if w else 0); wp = w
    return np.array(rets), np.array(tos), np.array(inv)

def overlay(rets, tos, fee=0.0008):
    eq = START_USD; peak = eq; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak; gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1+g*rets[t]-g*tos[t]*fee); peak = max(peak, eq); out.append(eq); rv.append(g*rets[t])
    return np.array(out)

rets, tos, inv_arr = signal_returns()
eq = overlay(rets, tos)
r_all = eq[1:]/eq[:-1]-1
yr_days = YEAR[22:22+len(rets)]           # align with the warmup-trimmed rets

d0 = datetime.fromtimestamp(days[0]*DAY/1000, tz=timezone.utc).date()
d1 = datetime.fromtimestamp(days[-1]*DAY/1000, tz=timezone.utc).date()
print(f"\nCARRY · production code · trailing-avg funding · 8bps · live overlay")
print(f"{len(coins)} coins · {d0} → {d1} ({YRS:.1f}yr)\n")
print(f"{'year':<8}{'return':>9}{'maxDD':>8}{'Sharpe':>8}{'invested%':>11}{'end equity':>13}")
print("─"*57)

def sh(x): return x.mean()/x.std()*np.sqrt(365) if len(x)>5 and x.std()>0 else 0.0
prev_eq = START_USD; yrs = sorted(set(yr_days))
for y in yrs:
    m = yr_days == y
    if m.sum() < 10: tag = "*";
    else: tag = ""
    yr_eq = np.array([prev_eq] + list(eq[1:][m]))
    yr_ret = yr_eq[-1]/yr_eq[0]-1
    yr_dd = ((np.maximum.accumulate(yr_eq)-yr_eq)/np.maximum.accumulate(yr_eq)).max()
    yr_sh = sh(r_all[m])
    yr_inv = inv_arr[m].mean()*100
    print(f"{str(y)+tag:<8}{yr_ret*100:>+8.0f}%{yr_dd*100:>7.0f}%{yr_sh:>8.2f}{yr_inv:>10.0f}%{yr_eq[-1]:>12,.0f}")
    prev_eq = yr_eq[-1]

print("─"*57)
def mdd(e): return ((np.maximum.accumulate(e)-e)/np.maximum.accumulate(e)).max()
cagr = (eq[-1]/eq[0])**(1/YRS)-1
print(f"TOTAL   CAGR {cagr*100:>+.0f}%  ·  Sharpe {sh(r_all):.2f}  ·  maxDD {mdd(eq)*100:.0f}%  ·  ${eq[-1]:,.0f}")
print(f"\n* partial year  ·  invested% = days with ≥1 open position")
