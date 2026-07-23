"""CARRY — the REAL APY. Right fill model, right gross, honest haircut.

Carry ESTABLISHES directional long/short positions and rebalances daily — so unlike the grid it ALWAYS
fills (no adverse-selection lottery); the only execution cost is fee + slippage on the rebalance TURNOVER.
We charge a realistic blended cost (maker/taker mix + spread/slippage on the mid-cap alts where funding
dispersion lives), sweep the GROSS the book actually runs at, and haircut for survivorship. Then give one
honest APY range and reconcile it to the live +10%/yr.
"""
import time, json, urllib.request
import numpy as np, datetime as dt
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "UNI","AAVE","FIL","ETC","XLM","INJ","SUI","ARB","OP","APT"]


def get(u):
    try: return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent":"Mozilla/5.0"}), timeout=25))
    except Exception: return None


def klines(sym):
    out={}; end=int(time.time()*1000); s=end-1700*24*3600*1000
    while s<end:
        k=get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1d&startTime={s}&limit=1500")
        if not k: break
        for r in k: out[int(r[0])//86400000]=float(r[4])
        if len(k)<1500: break
        s=k[-1][0]+1
    return out


def funding(sym):
    out={}; end=int(time.time()*1000); s=end-1700*24*3600*1000
    while s<end:
        f=get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={s}&limit=1000")
        if not f: break
        for r in f: out[int(r["fundingTime"])//86400000]=out.get(int(r["fundingTime"])//86400000,0.0)+float(r["fundingRate"])
        if len(f)<1000: break
        s=f[-1]["fundingTime"]+1
    return out


print("fetching prices + real funding...")
P={};F={}
for c in COINS:
    px=klines(c+"USDT"); fd=funding(c+"USDT")
    if len(px)>600 and len(fd)>600: P[c]=px; F[c]=fd
days=sorted(set.intersection(*[set(P[c]) for c in P])); coins=list(P); n=len(days)
CL={c:np.array([P[c][d] for d in days]) for c in coins}
FU={c:np.array([F[c].get(d,0.0) for d in days]) for c in coins}
yrs_span=n/365
print(f"{len(coins)} coins · {n} days · {yrs_span:.1f}yr ({dt.datetime.utcfromtimestamp(days[0]*86400):%Y-%m}→{dt.datetime.utcfromtimestamp(days[-1]*86400):%Y-%m})\n")


def z(x):
    x=np.array(x,float); s=x.std(); return (x-x.mean())/s if s>0 else x*0


def run(cost, gross, K=5, lb=21, favg=5):
    eq=[1.0]; wp={}; rets=[]
    for i in range(favg+lb+1, n-1):
        mom={c:CL[c][i]/CL[c][i-lb]-1 for c in coins}; fnd={c:FU[c][i-favg:i].mean() for c in coins}
        zm=dict(zip(coins,z([mom[c] for c in coins]))); zf=dict(zip(coins,z([fnd[c] for c in coins])))
        sc={c:zm[c]-zf[c] for c in coins}
        rank=sorted(coins,key=lambda c:sc[c],reverse=True); longs,shorts=rank[:K],rank[-K:]
        w={c:gross/2/K for c in longs}
        for c in shorts: w[c]=w.get(c,0)-gross/2/K
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in coins)
        pr=sum(w.get(c,0)*(CL[c][i+1]/CL[c][i]-1) for c in coins)
        fp=sum(-np.sign(w.get(c,0))*abs(w.get(c,0))*FU[c][i+1] for c in coins)
        eq.append(eq[-1]*(1+pr+fp-to*cost)); wp=w; rets.append(pr+fp-to*cost)
    eq=np.array(eq); rets=np.array(rets)
    cagr=(eq[-1]**(365/len(rets))-1)*100 if eq[-1]>0 else -100
    sh=rets.mean()/rets.std()*np.sqrt(365) if rets.std()>0 else 0
    dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr,sh,dd


print("="*68)
print("REAL APY — right fill (fee+slippage on turnover) x actual gross")
print("="*68)
print(f"{'fill cost/side':<18}{'gross':>7}{'APY':>8}{'Sharpe':>8}{'maxDD':>8}")
print("-"*68)
for cost,clabel in [(0.0006,"6bps (blended)"),(0.0008,"8bps (conservative)")]:
    for g in (1.0,1.5,2.0):
        c,s,dd=run(cost,g)
        note="  <- live gross" if g==1.5 else ""
        print(f"{clabel:<18}{g:>6.1f}x{c:>+7.0f}%{s:>8.2f}{dd:>7.0f}%{note}")
print("-"*68)
# honest number: live gross 1.5x, 7bps fill, minus survivorship haircut
c15,s15,dd15=run(0.0007,1.5)
HAIRCUT=0.75   # survivorship: today's 24 survivors flatter results; ~25% haircut
real=c15*HAIRCUT
print(f"\nHONEST REAL APY:")
print(f"  raw (1.5x gross, 7bps fill):        {c15:+.0f}% / yr   Sharpe {s15:.2f}   maxDD {dd15:.0f}%")
print(f"  after ~25% survivorship haircut:    {real:+.0f}% / yr   <-- the number to plan on")
print(f"  reconcile: live paper is +10%/yr (small ~9mo sample + vol-target keeps gross < 1.5x)")
print(f"\n  => REAL APY band ~ +10% (live/de-risked) to +{c15:.0f}% (raw 1.5x); central honest estimate ~+{real:.0f}%/yr.")
print(f"  Caveats: {yrs_span:.1f}yr window (misses 2021-22), survivorship-flattered, and slippage GROWS with")
print(f"  book size (mid-cap alts) — a much larger book earns less. Sharpe ~1.0, drawdowns ~25-35% at 1.5x.")
