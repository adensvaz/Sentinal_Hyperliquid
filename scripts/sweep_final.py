"""Final: tune risk overlays (vol-target, DD-throttle, leverage) on the CHAMPION (dual top5 mom + regime,
daily) to cut the ~55% drawdown while keeping the edge. Full + walk-forward halves."""
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
ser={c:d for c,d in (r for r in pmap(f,COINS,workers=10) if r)}
ref=max(ser,key=lambda c:len(ser[c])); T=sorted(ser[ref]); P={}; good=[]
for c in ser:
    a,have,lc=[],0,None
    for t in T:
        if t in ser[c]:lc=ser[c][t];have+=1
        a.append(lc)
    if have/len(T)>=.9 and a[0] is not None: P[c]=np.array(a,float);good.append(c)
n=len(T); FEE=0.0004; W=110
def met(eq):
    eq=np.array(eq); r=eq[1:]/eq[:-1]-1
    cagr=((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1]>0 else -100
    sh=r.mean()/r.std()*np.sqrt(365) if r.std()>0 else 0
    dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr,sh,dd
def reg(i,m): return P["BTC"][i]>P["BTC"][i-m:i].mean()
def champ_w(i,k=5,lb=30,regm=100):
    if not reg(i,regm): return {}
    s=[c for c in sorted(good,key=lambda c:P[c][i]/P[c][i-lb]-1,reverse=True) if P[c][i]>P[c][i-50:i].mean()][:k]
    return {c:1.0/k for c in s} if s else {}
def run(target_vol,ddth,lev,a,b):
    eq=[1.0];wp={};rets=[];peak=1.0
    for i in range(a+W,b-1):
        w=champ_w(i); scale=lev
        if target_vol and len(rets)>=20:
            rv=np.std(rets[-20:])*np.sqrt(365)
            if rv>0: scale=min(scale,lev*target_vol/rv)
        if ddth and (peak-eq[-1])/peak>ddth: scale*=0.35
        w={c:v*scale for c,v in w.items()}
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in good)
        ret=sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i]>0)
        net=ret-to*FEE; eq.append(eq[-1]*(1+net));rets.append(net);peak=max(peak,eq[-1]);wp=w
    return eq
def sc(tv,dd,lev):
    fl=met(run(tv,dd,lev,0,n)); h1=met(run(tv,dd,lev,0,n//2)); h2=met(run(tv,dd,lev,n//2,n))
    return fl,min(h1[1],h2[1])
print(f"{len(good)} coins, {n} bars\n")
print("CHAMPION = dual top-5 momentum (lb30) + BTC-regime(100) + risk overlays:")
print(f"   {'overlay':<34}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'worstHalf':>11}")
for lab,tv,dd,lev in [
  ("none (raw champion)",None,None,1.0),
  ("vol-target 50%",0.50,None,1.0),
  ("vol-target 40%",0.40,None,1.0),
  ("vol-target 40% + DD-throttle 25%",0.40,0.25,1.0),
  ("vol-target 30% + DD-throttle 20%",0.30,0.20,1.0),
  ("vol-target 50% @ 1.5x lev",0.50,0.25,1.5),
  ("vol-target 40% @ 2x lev",0.40,0.22,2.0)]:
    fl,wh=sc(tv,dd,lev); print(f"   {lab:<34}{fl[0]:>+6.0f}%{fl[1]:>8.2f}{fl[2]:>6.0f}%{wh:>11.2f}")
