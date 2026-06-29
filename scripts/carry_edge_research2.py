"""Carry edge research round 2 — lessons from round 1:
Funding gate & momentum filter DESTROY alpha by over-filtering the very signal we're harvesting.
The carry premium IS the edge. Right fixes are RISK MANAGEMENT, not signal filtering.

Risk parity (B from round 1) WORKS: Sharpe +0.09, DD 42%→28%. That's the direction.
Now we test the right class of improvement:

  D) SOFT GROSS SCALE BY FUNDING RICHNESS: instead of binary on/off gate, scale position
     size continuously by how rich funding is. Thin funding = half size. Rich = full size.
     Preserves alpha in the good years, just trades smaller in quiet ones.

  E) VOLATILITY REGIME SCALING: crypto squeezes happen in high-volatility markets.
     When BTC 20-day vol > threshold, scale down gross. Lower vol = can hold full position.
     Directly targets the 37% DD from 2021's violent moves.

  F) PER-NAME STOP LOSS: if any single shorted coin explodes >10% in one day against us,
     close that position. The squeeze tail is the single biggest risk in carry.
     This is point-in-time protection, doesn't filter the signal at all.

  G) CONCENTRATION CAP: never more than 25% of gross in any one name (enforced post risk-parity).
     Risk parity helps but on extreme moves a single coin can still dominate.

Best solo, then best combo, then heavy bootstrap.
"""
import sys, httpx, time as _t, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(9)
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
YRS = n/365.0; START = 10_000.0; K=5; FEE=0.0008
YEAR = np.array([datetime.fromtimestamp(d*DAY/1000, tz=timezone.utc).year for d in days])
# precompute BTC vol for regime (D fix)
BTC_VOL = np.array([P["BTC"][max(0,i-19):i+1] for i in range(n)], dtype=object)
def btc_vol(i, k=20):
    a = P["BTC"][max(0,i-k):i+1]; r = a[1:]/a[:-1]-1; r = r[~np.isnan(r)]
    return r.std()*np.sqrt(365) if len(r)>4 else 0.5

def avgF(c, i, look=5):
    a = F[c][max(0,i-look+1):i+1]; a = a[~np.isnan(a)]
    return a.mean() if len(a) else np.nan
def recentVol(c, i, k=20):
    cl = P[c][max(0,i-k):i+1]; r = cl[1:]/cl[:-1]-1; r = r[~np.isnan(r)]
    return max(r.std(), 1e-4) if len(r)>2 else 0.05

def run(risk_parity=False, vol_regime=False, per_name_stop=False,
        soft_fund_scale=False, conc_cap=0.0):
    rets, tos, inv = [], [], []; wp = {}; pos_entry = {}  # for per-name stop
    for i in range(21, n-1):
        elig = [c for c in coins if av[c][i] and av[c][i+1] and not np.isnan(avgF(c, i))]
        if len(elig) < 12:
            tos.append(sum(abs(wp.get(c,0)) for c in coins)); rets.append(0.0); inv.append(0); wp={}; continue

        # ── D: soft gross scale by funding richness ─────────────────────────────────────────
        gross_mult = 1.0
        if soft_fund_scale:
            universe_avg = abs(np.mean([avgF(c, i) for c in elig]))
            # scale: 0.5x at very thin funding, 1.0x at typical richness, smooth ramp
            # typical daily sum ≈ 0.0003-0.0009 (0.01-0.03% × 3 per day)
            thin_thr, rich_thr = 0.0001, 0.0006
            if universe_avg >= rich_thr: gross_mult = 1.0
            elif universe_avg <= thin_thr: gross_mult = 0.5
            else: gross_mult = 0.5 + 0.5*(universe_avg-thin_thr)/(rich_thr-thin_thr)

        # ── E: volatility regime — scale down in high-vol environments ────────────────────
        if vol_regime:
            bvol = btc_vol(i, 20)   # annualised BTC vol
            if bvol > 1.20:   gross_mult *= 0.50   # extreme vol (>120% ann) = high squeeze risk
            elif bvol > 0.80: gross_mult *= 0.75   # elevated vol

        rank = sorted(elig, key=lambda c: avgF(c, i)); longs=rank[:K]; shorts=rank[-K:]

        # ── B: risk parity sizing (kept from round 1 — confirmed winner) ─────────────────
        if risk_parity:
            def rw(g):
                vols = {c: recentVol(c,i) for c in g}; iv = {c:1/v for c,v in vols.items()}
                s = sum(iv.values())
                wts = {c: iv[c]/s for c in g}
                # ── G: concentration cap ──────────────────────────────────────────────────
                if conc_cap > 0:
                    for _ in range(8):
                        over = [c for c in wts if wts[c] > conc_cap+1e-9]
                        if not over: break
                        exc = sum(wts[c]-conc_cap for c in over)
                        for c in over: wts[c] = conc_cap
                        und = [c for c in wts if wts[c]<conc_cap-1e-9]; base=sum(wts[c] for c in und)
                        if base<=0: break
                        for c in und: wts[c]+=exc*wts[c]/base
                return wts
            wl=rw(longs); ws=rw(shorts)
            w = {c: gross_mult*0.5*wl[c] for c in longs}
            for c in shorts: w[c] = w.get(c,0) - gross_mult*0.5*ws[c]
        else:
            w = {c: gross_mult/K for c in longs}
            for c in shorts: w[c] = w.get(c,0) - gross_mult/K

        # ── F: per-name stop loss — close any short that exploded >10% today against us ──
        if per_name_stop and wp:
            for c, wgt in list(w.items()):
                if wgt < -1e-9 and av[c][i] and av[c][i-1] and P[c][i-1]>0:
                    day_move = P[c][i]/P[c][i-1]-1
                    if day_move > 0.10:   # our short ripped >10% → exit it
                        w[c] = 0.0
        # zero out positions on coins that just delisted
        w = {c:v for c,v in w.items() if av[c][i]}
        to = sum(abs(w.get(c,0)-wp.get(c,0)) for c in coins)
        pr = sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in coins if w.get(c) and av[c][i] and av[c][i+1])
        fn = sum(-w.get(c,0)*F[c][i+1] for c in coins if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); tos.append(to); inv.append(1 if w else 0); wp=w
    return np.array(rets), np.array(tos), np.array(inv)

def overlay(rets, tos):
    eq=START; peak=eq; out=[eq]; rv=[]
    for t in range(len(rets)):
        vol=np.std(rv[-21:]) if len(rv)>=5 else 0.0
        gv=min(1.0,0.018/vol) if vol>1e-9 else 1.0
        dd=1-eq/peak; gd=1.0 if dd<=0.12 else (0.25 if dd>=0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g=max(0.25,min(1.0,gv,gd))
        eq*=(1+g*rets[t]-g*tos[t]*FEE); peak=max(peak,eq); out.append(eq); rv.append(g*rets[t])
    return np.array(out)
def met(eq):
    r=eq[1:]/eq[:-1]-1; h=len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std()>0 else 0
    cagr=(eq[-1]/eq[0])**(1/YRS)-1; dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    return cagr,sh(r),dd,min(sh(r[:h]),sh(r[h:]))

def per_year(eq, rets_arr):
    yr_days=YEAR[22:22+len(rets_arr)]; prev=START; rows={}
    for y in sorted(set(yr_days)):
        m=yr_days==y; yr_eq=np.array([prev]+list(eq[1:][m]))
        rows[y]=(yr_eq[-1]/yr_eq[0]-1)*100; prev=yr_eq[-1]
    return rows

VARIANTS = [
    ("BASELINE (current, no risk-parity)",    dict()),
    ("B) risk parity only (round-1 winner)",  dict(risk_parity=True)),
    ("D) soft fund scale only",               dict(soft_fund_scale=True)),
    ("E) vol regime only",                    dict(vol_regime=True)),
    ("F) per-name stop (>10%) only",          dict(per_name_stop=True)),
    ("G) conc cap 25% only",                  dict(risk_parity=True, conc_cap=0.25)),
    ("B+D) risk-par + soft-scale",            dict(risk_parity=True, soft_fund_scale=True)),
    ("B+E) risk-par + vol-regime",            dict(risk_parity=True, vol_regime=True)),
    ("B+F) risk-par + stop-loss",             dict(risk_parity=True, per_name_stop=True)),
    ("B+G) risk-par + conc-cap 25%",          dict(risk_parity=True, conc_cap=0.25)),
    ("B+D+E) rp + soft + vol",                dict(risk_parity=True, soft_fund_scale=True, vol_regime=True)),
    ("B+E+F) rp + vol + stop",                dict(risk_parity=True, vol_regime=True, per_name_stop=True)),
    ("B+D+E+F) ALL FOUR",                     dict(risk_parity=True, soft_fund_scale=True, vol_regime=True, per_name_stop=True)),
    ("B+D+E+F+G) FULL STACK",                 dict(risk_parity=True, soft_fund_scale=True, vol_regime=True, per_name_stop=True, conc_cap=0.25)),
]
print(f"\n{len(coins)} coins · {YRS:.1f}yr · 8bps · $10k\n")
print(f"{'variant':<38}{'CAGR':>6}{'Shrp':>6}{'maxDD':>7}{'wHalf':>7}{'$final':>10}")
print("─"*74)
res = {}
for lab,kw in VARIANTS:
    r,t,iv=run(**kw); eq=overlay(r,t); cagr,sh,dd,wh=met(eq); res[lab]=(eq,r,iv)
    marker = " ← best?" if sh > 2.10 else ""
    print(f"{lab:<38}{cagr*100:>+5.0f}%{sh:>6.2f}{dd*100:>6.0f}%{wh:>7.2f}   ${eq[-1]:>8,.0f}{marker}")
print("─"*74)

# pick the winner and show its per-year vs baseline
best_lab = max(res.keys(), key=lambda k: met(res[k][0])[1])  # highest Sharpe
print(f"\nPER-YEAR returns — BASELINE vs '{best_lab}'")
yr_b = per_year(res["BASELINE (current, no risk-parity)"][0], res["BASELINE (current, no risk-parity)"][1])
yr_x = per_year(res[best_lab][0], res[best_lab][1])
print(f"{'year':<7}{'BASE':>8}{'BEST':>8}{'Δ':>7}")
for y in sorted(yr_b): print(f"{y:<7}{yr_b[y]:>+7.0f}%{yr_x.get(y,0):>+7.0f}%{yr_x.get(y,0)-yr_b[y]:>+6.0f}pp")

print(f"\nBOOTSTRAP — BASELINE vs {best_lab} (100x random 18-coin subsets, full overlay)")
best_kw = dict(VARIANTS)[[v[0] for v in VARIANTS].index(best_lab)][1]
def bt_sub(sub, kw):
    r,t,iv=run(**{**kw}); return overlay(r,t)
shs_b,shs_x,dds_b,dds_x=[],[],[],[]
def run_sub(sub,kw):
    """Approximate sub-universe run — reuse global but restrict signals to sub."""
    rets,tos=[],[]; wp={}
    for i in range(21,n-1):
        elig=[c for c in sub if av[c][i] and av[c][i+1] and not np.isnan(avgF(c,i))]
        gm=1.0
        if kw.get("soft_fund_scale") and elig:
            ua=abs(np.mean([avgF(c,i) for c in elig])); th,rh=0.0001,0.0006
            gm=min(1.0,max(0.5,0.5+0.5*(ua-th)/(rh-th)))
        if kw.get("vol_regime"):
            bv=btc_vol(i); gm*=(0.50 if bv>1.2 else 0.75 if bv>0.8 else 1.0)
        if len(elig)<6: w={}
        else:
            rank=sorted(elig,key=lambda c:avgF(c,i)); L=rank[:3]; S=rank[-3:]
            if kw.get("risk_parity"):
                def rw(g): vols={c:recentVol(c,i) for c in g}; iv={c:1/v for c,v in vols.items()}; s=sum(iv.values()); return {c:iv[c]/s for c in g}
                wl=rw(L); ws=rw(S); w={c:gm*0.5*wl[c] for c in L}
                for c in S: w[c]=w.get(c,0)-gm*0.5*ws[c]
            else:
                w={c:gm/3 for c in L}
                for c in S: w[c]=w.get(c,0)-gm/3
            if kw.get("per_name_stop") and wp:
                for c,wgt in list(w.items()):
                    if wgt<-1e-9 and i>0 and av[c][i] and av[c][i-1] and P[c][i-1]>0:
                        if P[c][i]/P[c][i-1]-1>0.10: w[c]=0.0
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in sub)
        pr=sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in sub if w.get(c) and av[c][i] and av[c][i+1])
        fn=sum(-w.get(c,0)*F[c][i+1] for c in sub if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); tos.append(to); wp=w
    return overlay(np.array(rets),np.array(tos))
for _ in range(100):
    sub=list(rng.choice(coins,size=min(18,len(coins)),replace=False))
    if "BTC" not in sub: sub[0]="BTC"
    eqb=run_sub(sub,{}); eqx=run_sub(sub,best_kw)
    cb,sb,db,wb=met(eqb); cx,sx,dx,wx=met(eqx)
    shs_b.append(sb); shs_x.append(sx); dds_b.append(db); dds_x.append(dx)
shs_b=np.array(shs_b); shs_x=np.array(shs_x); dds_b=np.array(dds_b); dds_x=np.array(dds_x)
print(f"BASELINE  Sharpe {shs_b.mean():.2f} | maxDD {dds_b.mean()*100:.0f}%")
print(f"BEST      Sharpe {shs_x.mean():.2f} | maxDD {dds_x.mean()*100:.0f}% | Sharpe wins {100*(shs_x>shs_b).mean():.0f}% · DD wins {100*(dds_x<dds_b).mean():.0f}%")
print("─"*74)
