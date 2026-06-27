"""EXHAUSTIVE sweep on 5.5yr Binance data: all archetypes, full trend x regime grid, concentration,
cadence. Each scored full-period AND on both walk-forward halves (robustness = worst half)."""
import sys, time as _t, httpx, numpy as np
sys.path.insert(0,"src")
from sentinel.util.concurrency import pmap
COINS=["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
       "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
def kl(s,end=None):
    p={"symbol":s+"USDT","interval":"1d","limit":1000}
    if end:p["endTime"]=end
    return httpx.get("https://data-api.binance.vision/api/v3/klines",params=p,timeout=20).json()
def f(c):
    p1=kl(c)
    if not isinstance(p1,list) or not p1:return None
    p2=kl(c,end=int(p1[0][0])-1)
    return c,{int(r[0]):float(r[4]) for r in (p2 if isinstance(p2,list) else [])+p1}
print("loading 5.5yr...")
ser={c:d for c,d in (r for r in pmap(f,COINS,workers=10) if r)}
ref=max(ser,key=lambda c:len(ser[c])); T=sorted(ser[ref]); P={}; good=[]
for c in ser:
    a,have,lc=[],0,None
    for t in T:
        if t in ser[c]:lc=ser[c][t];have+=1
        a.append(lc)
    if have/len(T)>=.9 and a[0] is not None: P[c]=np.array(a,float);good.append(c)
n=len(T); FEE=0.0004; W=100
def met(eq):
    eq=np.array(eq)
    if len(eq)<10: return (0,0,0)
    r=eq[1:]/eq[:-1]-1
    cagr=((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1]>0 else -100
    sh=r.mean()/r.std()*np.sqrt(365) if r.std()>0 else 0
    dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr,sh,dd
def run(wfn,a,b,every=1):
    eq=[1.0];wp={}
    for i in range(a+W,b-1):
        if (i-(a+W))%every==0: w=wfn(i)
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in good)
        ret=sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i]>0)
        eq.append(eq[-1]*(1+ret-to*FEE));wp=w
    return eq
def reg(i,m): return (m==0) or (P["BTC"][i]>P["BTC"][i-m:i].mean())
def score(wfn,every=1):
    full=met(run(wfn,0,n,every)); h1=met(run(wfn,0,n//2,every)); h2=met(run(wfn,n//2,n,every))
    return full,min(h1[1],h2[1])   # (full metrics, worst-half Sharpe = robustness)

# ---------- A) ARCHETYPE SHOOTOUT ----------
def A_buyhold(i): return {c:1/len(good) for c in good}
def A_trend(i):   # long above 50dMA + regime100 (the winner)
    if not reg(i,100): return {}
    up=[c for c in good if P[c][i]>P[c][i-50:i].mean()]; return {c:1/len(good) for c in up} if up else {}
def A_csmom(i):   # top-8 by 30d return + regime
    if not reg(i,100): return {}
    s=sorted(good,key=lambda c:P[c][i]/P[c][i-30]-1,reverse=True)[:8]; return {c:1/8 for c in s}
def A_dual(i):    # top-8 relative strength that are ALSO in uptrend + regime
    if not reg(i,100): return {}
    s=[c for c in sorted(good,key=lambda c:P[c][i]/P[c][i-30]-1,reverse=True)[:8] if P[c][i]>P[c][i-50:i].mean()]
    return {c:1/8 for c in s} if s else {}
def A_breakout(i):# long at 50d high + regime
    if not reg(i,100): return {}
    up=[c for c in good if P[c][i]>=P[c][i-50:i+1].max()]; return {c:1/len(good) for c in up} if up else {}
def A_meanrev(i): # long 8 most-oversold (below 20d mean) + regime
    if not reg(i,100): return {}
    s=sorted(good,key=lambda c:P[c][i]/P[c][i-20:i].mean())[:8]; return {c:1/8 for c in s}
def A_ls(i):      # long uptrend / short downtrend, balanced (no regime)
    up=[c for c in good if P[c][i]>P[c][i-50:i].mean()];dn=[c for c in good if P[c][i]<=P[c][i-50:i].mean()]
    w={}; [w.__setitem__(c,0.5/max(len(up),1)) for c in up]; [w.__setitem__(c,-0.5/max(len(dn),1)) for c in dn]; return w
print(f"\n{len(good)} coins, {n} bars  ·  FEE {FEE}\n")
print("A) ARCHETYPE SHOOTOUT")
print(f"   {'archetype':<22}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'worst-half Sh':>15}")
for lab,fn in [("buy-hold basket",A_buyhold),("trend+regime (winner)",A_trend),("cross-sec mom+regime",A_csmom),
               ("dual momentum+regime",A_dual),("breakout+regime",A_breakout),("mean-reversion+regime",A_meanrev),
               ("long/short trend",A_ls)]:
    fl,wh=score(fn); print(f"   {lab:<22}{fl[0]:>+6.0f}%{fl[1]:>8.2f}{fl[2]:>6.0f}%{wh:>15.2f}")

# ---------- B) TREND x REGIME GRID (robustness surface = worst-half Sharpe) ----------
print("\nB) TREND-MA x REGIME-MA GRID  (cells = worst-half Sharpe; robust edge = broad green region)")
regs=[0,50,100,150,200]; trends=[20,30,50,75,100,150]
print("   trendMA\\regMA  "+"".join(f"{('off' if r==0 else r):>7}" for r in regs))
for tm in trends:
    row=[]
    for rm in regs:
        def wfn(i,tm=tm,rm=rm):
            if not reg(i,rm): return {}
            up=[c for c in good if P[c][i]>P[c][i-tm:i].mean()]; return {c:1/len(good) for c in up} if up else {}
        _,wh=score(wfn); row.append(wh)
    print(f"   {tm:>10}    "+"".join(f"{v:>7.2f}" for v in row))

# ---------- C) CONCENTRATION (top-K strongest trends, regime100) ----------
print("\nC) CONCENTRATION (long top-K by trend strength, regime100)")
for k in [3,5,8,12,26]:
    def wfn(i,k=k):
        if not reg(i,100): return {}
        s=[c for c in sorted(good,key=lambda c:P[c][i]/P[c][i-50]-1,reverse=True) if P[c][i]>P[c][i-50:i].mean()][:k]
        return {c:1/k for c in s} if s else {}
    fl,wh=score(wfn); print(f"   top-{k:<3} CAGR {fl[0]:>+5.0f}%  Sh {fl[1]:.2f}  DD {fl[2]:.0f}%  worst-half {wh:.2f}")

# ---------- D) CADENCE ----------
print("\nD) REBALANCE CADENCE (winner)")
for ev,lab in [(1,"daily"),(3,"3-day"),(7,"weekly")]:
    fl,wh=score(A_trend,ev); print(f"   {lab:<7} CAGR {fl[0]:>+5.0f}%  Sh {fl[1]:.2f}  DD {fl[2]:.0f}%  worst-half {wh:.2f}")
