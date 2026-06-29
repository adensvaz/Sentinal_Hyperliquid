"""DEFINITIVE risk-parity-blend test at the EXACT production gross (target_gross=2.0 → ±1.0 per sleeve).
Tests weight ∝ (1/vol)^β for β in {0, 0.3, 0.5, 0.75, 1.0} on the production combo signal.
One gross convention, full path + bootstrap. Decides the default β to ship."""
import sys, httpx, time as _t, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
from sentinel.strategy.funding_carry import carry_rank
rng = np.random.default_rng(13)
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ",
         "TRX","FTM","MANA","AXS","EGLD","CHZ","ENJ","DASH","WAVES","COMP","SUSHI","GALA"]
DAY = 86400000
def kl(c, end=None):
    p = {"symbol": c+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    try: return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=25).json()
    except Exception: return []
def ph(c):
    out, end = {}, None
    for _ in range(5):
        rows = kl(c, end)
        if not isinstance(rows, list) or not rows: break
        for r in rows: out[int(r[0])//DAY] = float(r[4])
        end = int(rows[0][0])-1
        if len(rows) < 1000: break
    return (c, out) if len(out) > 200 else None
def fh(c):
    out, s = {}, 1530000000000
    for _ in range(40):
        p = {"symbol": c+"USDT", "limit": 1000, "startTime": s}
        try: rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params=p, timeout=25).json()
        except Exception: break
        if not isinstance(rows, list) or not rows: break
        for x in rows: out.setdefault(int(x["fundingTime"])//DAY, 0.0); out[int(x["fundingTime"])//DAY] += float(x["fundingRate"])
        if len(rows) < 1000: break
        s = int(rows[-1]["fundingTime"])+1; _t.sleep(0.12)
    return (c, out) if len(out) > 150 else None
print("fetching…")
PX = {c: d for c, d in (r for r in pmap(ph, COINS, workers=10) if r)}
FD = {c: d for c, d in (r for r in pmap(fh, [c for c in COINS if c in PX], workers=4) if r)}
coins = [c for c in PX if c in FD]
days = sorted(set().union(*[set(PX[c]) for c in coins]))
P = {c: np.array([PX[c].get(d, np.nan) for d in days], float) for c in coins}
F = {c: np.array([FD[c].get(d, np.nan) for d in days], float) for c in coins}
av = {c: (~np.isnan(P[c])) & (P[c] > 0) & (~np.isnan(F[c])) for c in coins}
nc = np.array([sum(av[c][i] for c in coins) for i in range(len(days))])
s0 = int(np.argmax(nc >= 8)); days = days[s0:]; n = len(days)
for c in coins: P[c], F[c], av[c] = P[c][s0:], F[c][s0:], av[c][s0:]
YRS = n/365.0; FEE = 0.0008
def avgF(c, i, k=5):
    a = F[c][max(0, i-k+1):i+1]; a = a[~np.isnan(a)]; return a.mean() if len(a) else np.nan
def mom(c, i, lb=21):
    if i-lb < 0 or not av[c][i] or not av[c][i-lb] or P[c][i-lb] <= 0: return np.nan
    return P[c][i]/P[c][i-lb]-1
def rVol(c, i, k=20):
    cl = P[c][max(0, i-k):i+1]; r = cl[1:]/cl[:-1]-1; r = r[~np.isnan(r)]
    return max(r.std(), 1e-4) if len(r) > 2 else 0.05
def run(sub, beta, K=5):
    rets, tos = [], []; wp = {}
    for i in range(21, n-1):
        cand = [(c, mom(c, i), avgF(c, i)) for c in sub
                if av[c][i] and av[c][i+1] and not np.isnan(mom(c, i)) and not np.isnan(avgF(c, i))]
        if len(cand) < 2*K: w = {}
        else:
            ranked = carry_rank(cand); L = ranked[:K]; S = ranked[-K:]
            def wt(g):                       # weights sum to 1.0 per sleeve  → ±1.0 = production gross 2.0
                iv = {c: (1.0/rVol(c, i))**beta for c in g}; ss = sum(iv.values())
                return {c: iv[c]/ss for c in g}
            wl, ws = wt(L), wt(S)
            w = {c: wl[c] for c in L}          # long sleeve sums to +1.0
            for c in S: w[c] = w.get(c, 0) - ws[c]   # short sleeve sums to -1.0
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in sub)
        pr = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in sub if w.get(c) and av[c][i] and av[c][i+1])
        fn = sum(-w.get(c, 0)*F[c][i+1] for c in sub if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); tos.append(to); wp = w
    eq = 10000.0; peak = 10000.0; eql = [10000.0]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak; gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd)); eq *= (1+g*rets[t]-g*tos[t]*FEE); peak = max(peak, eq); eql.append(eq); rv.append(g*rets[t])
    return np.array(eql)
def met(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2; sh = lambda x: x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = (eq[-1]/eq[0])**(1/YRS)-1; dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    return cagr, sh(r), dd
print(f"\nPRODUCTION-GROSS blend test (±1.0/sleeve = target_gross 2.0) · {YRS:.1f}yr · combo signal\n")
print(f"{'β':>5}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>7}   bootstrap(60x): Sh / meanDD / Sh-wins / DD-wins")
print("─"*78)
# precompute baseline bootstrap (β=0)
base_sh, base_dd = {}, {}
subs = []
for _ in range(60):
    sub = list(rng.choice(coins, size=18, replace=False))
    if "BTC" not in sub: sub[0] = "BTC"
    subs.append(sub)
b0 = [met(run(s, 0.0, K=3)) for s in subs]
b0_sh = np.array([x[1] for x in b0]); b0_dd = np.array([x[2] for x in b0])
for beta in [0.0, 0.3, 0.5, 0.75, 1.0]:
    cf, sf, df = met(run(coins, beta))
    bb = [met(run(s, beta, K=3)) for s in subs]
    bsh = np.array([x[1] for x in bb]); bdd = np.array([x[2] for x in bb])
    shw = 100*(bsh > b0_sh).mean() if beta > 0 else 0
    ddw = 100*(bdd < b0_dd).mean() if beta > 0 else 0
    print(f"{beta:>5.2f}{cf*100:>+7.0f}%{sf:>8.2f}{df*100:>6.0f}%      Sh {bsh.mean():.2f} / DD {bdd.mean()*100:.0f}% / Sh-w {shw:.0f}% / DD-w {ddw:.0f}%")
print("─"*78)
print("KNEE = highest β where Sharpe still ~holds AND DD-wins is high. That β ships as the default.")
