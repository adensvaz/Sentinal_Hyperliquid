"""INNOVATE — FUNDING CARRY, done on real multi-year data.
Funding RATES: Binance funding history (forward-paginated, ~6.8yr, dense 3/day) -> summed to DAILY.
PRICES: daily klines back to 2018 (markPrice in old funding records is 0, so we use klines instead).
Strategy: dollar-neutral, SHORT highest trailing-funding coins (collect funding) / LONG lowest.
Daily return for signed weight w: w*(closeΔ) - w*(daily funding).  Carry = -w*funding (short earns on +funding).
Validated head-to-head vs momentum, per-year, fees, universe bootstrap."""
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
    out, start = {}, 1530000000000   # ~2018-06
    for _ in range(40):
        p = {"symbol": c+"USDT", "limit": 1000, "startTime": start}
        try: rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params=p, timeout=25).json()
        except Exception: break
        if not isinstance(rows, list) or not rows: break
        for x in rows: out.setdefault(int(x["fundingTime"])//DAY, 0.0); out[int(x["fundingTime"])//DAY] += float(x["fundingRate"])
        if len(rows) < 1000: break
        start = int(rows[-1]["fundingTime"])+1; _t.sleep(0.12)
    return (c, out) if len(out) > 150 else None

print("fetching daily prices…")
PX = {c: d for c, d in (r for r in pmap(price_hist, COINS, workers=10) if r)}
print(f"  prices for {len(PX)} coins")
print("fetching funding rates (low concurrency to avoid rate-limit)…")
FD = {c: d for c, d in (r for r in pmap(fund_hist, [c for c in COINS if c in PX], workers=4) if r)}
print(f"  funding for {len(FD)} coins")
coins = [c for c in PX if c in FD]
days = sorted(set().union(*[set(PX[c]) for c in coins]))
P = {c: np.array([PX[c].get(d, np.nan) for d in days], float) for c in coins}
F = {c: np.array([FD[c].get(d, np.nan) for d in days], float) for c in coins}
av = {c: (~np.isnan(P[c])) & (P[c] > 0) & (~np.isnan(F[c])) for c in coins}   # need BOTH price & funding
ncoin = np.array([sum(av[c][i] for c in coins) for i in range(len(days))])
start = int(np.argmax(ncoin >= 8))
days = days[start:]; n = len(days)
for c in coins: P[c], F[c], av[c] = P[c][start:], F[c][start:], av[c][start:]
YRS = n/365.0
YEAR = np.array([datetime.fromtimestamp(d*DAY/1000, tz=timezone.utc).year for d in days])
d0 = datetime.fromtimestamp(days[0]*DAY/1000, tz=timezone.utc).date()
d1 = datetime.fromtimestamp(days[-1]*DAY/1000, tz=timezone.utc).date()

def avgF(c, i, look=5):
    a = F[c][max(0, i-look+1):i+1]; a = a[~np.isnan(a)]
    return a.mean() if len(a) else np.nan
def mom(c, i, lb=21):
    return P[c][i]/P[c][i-lb]-1 if i-lb >= 0 and av[c][i] and av[c][i-lb] and P[c][i-lb] > 0 else np.nan

def bt(kind, K=5, ev=1, fee=0.0004, U=None):
    U = U or coins; eq = [1.0]; wp = {}; rets = []
    for i in range(21, n-1):
        if i % ev == 0 or not wp:
            elig = [c for c in U if av[c][i] and av[c][i+1]]
            if kind == "carry":
                elig = [c for c in elig if not np.isnan(avgF(c, i))]
                rank = sorted(elig, key=lambda c: avgF(c, i))           # low/neg funding -> long ; high -> short
            elif kind == "mom":
                elig = [c for c in elig if not np.isnan(mom(c, i))]
                rank = sorted(elig, key=lambda c: mom(c, i), reverse=True)
            else:  # combo: rank by momentum-rank + funding-rank (long hi-mom/lo-fund, short lo-mom/hi-fund)
                elig = [c for c in elig if not np.isnan(mom(c, i)) and not np.isnan(avgF(c, i))]
                if len(elig) >= 2*K+2:
                    mr = {c: r for r, c in enumerate(sorted(elig, key=lambda c: mom(c, i)))}
                    fr = {c: r for r, c in enumerate(sorted(elig, key=lambda c: -avgF(c, i)))}
                    rank = sorted(elig, key=lambda c: mr[c]+fr[c], reverse=True)
                else: rank = []
            if len(rank) >= 2*K+2:
                w = {c: 1.0/K for c in rank[:K]}
                for c in rank[-K:]: w[c] = w.get(c, 0)-1.0/K
            else: w = {}
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in U)
        pr = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in U if w.get(c) and av[c][i] and av[c][i+1])
        # NO look-ahead: signal uses funding<=i; we collect the NEXT day's funding (held i -> i+1)
        fn = sum(-w.get(c, 0)*F[c][i+1] for c in U if w.get(c) and not np.isnan(F[c][i+1]))
        eq.append(eq[-1]*(1+pr+fn-to*fee)); wp = w; rets.append(pr+fn-to*fee)
    return np.array(eq), np.array(rets)
def met(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = ((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))

print(f"\nCARRY study · {len(coins)} coins · {d0} -> {d1} ({YRS:.1f}yr) · {int(ncoin[start])}->{sum(av[c][-1] for c in coins)} coins\n")
print(f"{'strategy (daily, K=5/side, 4bps)':<38}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'wHalf':>7}")
print("-"*67)
res = {}
for k, lab in [("carry", "FUNDING CARRY (short high-funding)"), ("mom", "MOMENTUM (price)"), ("combo", "COMBO (mom + funding)")]:
    eq, r = bt(k); res[k] = r; c, s, dd, wh = met(eq)
    print(f"{lab:<38}{c:>+6.0f}%{s:>8.2f}{dd:>6.0f}%{wh:>7.2f}")
print("-"*67)
print("\n[per-year Sharpe]")
yrs = sorted(set(YEAR[22:]))
print("  "+"strat".ljust(8)+"".join(f"{y:>7}" for y in yrs))
for k in ("carry", "mom", "combo"):
    r = res[k]; ys = YEAR[22:22+len(r)]; row = ""
    for y in yrs:
        m = ys == y; row += f"{(r[m].mean()/r[m].std()*np.sqrt(365) if m.sum()>20 and r[m].std()>0 else 0):>7.2f}"
    print(f"  {k:<8}{row}")
print("\n[CARRY fee sensitivity]")
for bps in (4, 8, 15, 25):
    eq, _ = bt("carry", fee=bps/1e4); c, s, dd, wh = met(eq)
    print(f"  {bps:>2}bps: CAGR {c:>+4.0f}%  Sharpe {s:.2f}  maxDD {dd:.0f}%  wHalf {wh:.2f}")
print("\n[CARRY universe bootstrap — 80x random 20-coin subsets, 8bps]")
shs = []
for _ in range(80):
    sub = list(rng.choice(coins, size=min(20, len(coins)), replace=False))
    c, s, dd, wh = met(bt("carry", fee=0.0008, U=sub)[0]); shs.append(s)
shs = np.array(shs)
print(f"  Sharpe: mean {shs.mean():.2f} · >0.5 in {100*(shs>0.5).mean():.0f}% · >0 in {100*(shs>0).mean():.0f}% · [p5 {np.percentile(shs,5):.2f}, p95 {np.percentile(shs,95):.2f}]")
print("\n[correlation / market-neutrality]")
cr, mr = res["carry"], res["mom"]
btc = np.array([(P["BTC"][i+1]/P["BTC"][i]-1) if av["BTC"][i] and av["BTC"][i+1] else 0.0 for i in range(21, n-1)])
L = min(len(cr), len(mr), len(btc)); cr, mr, btc = cr[:L], mr[:L], btc[:L]
print(f"  corr(carry, momentum) = {np.corrcoef(cr, mr)[0,1]:+.2f}  (low = genuinely different edge / diversifier)")
print(f"  corr(carry, BTC)      = {np.corrcoef(cr, btc)[0,1]:+.2f}  (near 0 = market-neutral as intended)")
print("-"*67)
print("VERDICT: a real carry edge = positive Sharpe, holds per-year, survives fees, robust to universe.")
