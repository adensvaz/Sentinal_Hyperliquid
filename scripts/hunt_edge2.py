"""Refine the winner (trend + BTC-regime filter): add vol-targeting + drawdown throttle + momentum tilt.
Goal: push Sharpe up and the 41% drawdown DOWN, staying robust in BOTH walk-forward halves."""
import sys, time as _t, httpx, numpy as np
sys.path.insert(0,"src")
from sentinel.util.concurrency import pmap
COINS=["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
       "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
def kl(s,end=None):
    p={"symbol":s+"USDT","interval":"1d","limit":1000}
    if end:p["endTime"]=end
    return httpx.get("https://data-api.binance.vision/api/v3/klines",params=p,timeout=20).json()
def fetch(c):
    p1=kl(c)
    if not isinstance(p1,list) or not p1:return None
    p2=kl(c,end=int(p1[0][0])-1)
    return c,{int(r[0]):float(r[4]) for r in (p2 if isinstance(p2,list) else [])+p1}
ser={c:d for c,d in (r for r in pmap(fetch,COINS,workers=10) if r)}
ref=max(ser,key=lambda c:len(ser[c])); T=sorted(ser[ref]); P={}; good=[]
for c in ser:
    a,have,lc=[],0,None
    for t in T:
        if t in ser[c]: lc=ser[c][t];have+=1
        a.append(lc)
    if have/len(T)>=.9 and a[0] is not None: P[c]=np.array(a,float);good.append(c)
n=len(T); FEE=0.0004
print(f"{len(good)} coins, {n} bars\n")
def above(c,i,w): return P[c][i]>P[c][i-w:i].mean()
def regime(i): return P["BTC"][i]>P["BTC"][i-100:i].mean()
def met(eq):
    eq=np.array(eq); r=eq[1:]/eq[:-1]-1
    cagr=(eq[-1]/eq[0])**(365/len(eq))-1 if eq[-1]>0 else -1
    sh=r.mean()/r.std()*np.sqrt(365) if r.std()>0 else 0
    dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr*100,sh,dd
def base_w(i,tilt=False):
    if not regime(i): return {}
    up=[c for c in good if above(c,i,50)]
    if not up: return {}
    if tilt:  # overweight the strongest-trending names among the uptrend set
        sc={c:max(P[c][i]/P[c][i-60]-1,0) for c in up}; s=sum(sc.values()) or 1
        return {c:min(sc[c]/s,0.2) for c in up}
    return {c:1.0/len(good) for c in up}
def run(target_vol=None, ddth=None, tilt=False, lev=1.0, a=0, b=None):
    b=b or n; eq=[1.0]; w_prev={}; rets=[]; peak=1.0
    for i in range(a+100,b-1):
        w=base_w(i,tilt); scale=lev
        if target_vol and len(rets)>=20:
            rv=np.std(rets[-20:])*np.sqrt(365)
            if rv>0: scale=min(scale, lev*target_vol/rv)
        if ddth:
            dd=(peak-eq[-1])/peak
            if dd>ddth: scale*=0.4
        w={c:v*scale for c,v in w.items()}
        to=sum(abs(w.get(c,0)-w_prev.get(c,0)) for c in good)
        ret=sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i]>0)
        net=ret-to*FEE; eq.append(eq[-1]*(1+net)); rets.append(net); peak=max(peak,eq[-1]); w_prev=w
    return eq
def show(lab,**kw):
    f=met(run(**kw,a=0,b=n)); h1=met(run(**kw,a=0,b=n//2)); h2=met(run(**kw,a=n//2,b=n))
    print(f"  {lab:<28} CAGR {f[0]:+5.0f}% Sh {f[1]:5.2f} DD {f[2]:3.0f}%  | H1 Sh {h1[1]:5.2f} H2 Sh {h2[1]:5.2f}")
print("=== REFINING THE WINNER (trend + BTC regime) ===")
show("base winner")
show("+ vol-target 35%", target_vol=0.35)
show("+ vol-target 35% + DD-throttle", target_vol=0.35, ddth=0.18)
show("+ vol-target + DD + mom-tilt", target_vol=0.35, ddth=0.18, tilt=True)
show("+ vol-target 50% (more punch)", target_vol=0.50, ddth=0.20)
show("+ vol-target 35% @ 1.5x lev", target_vol=0.35, ddth=0.18, lev=1.5)
