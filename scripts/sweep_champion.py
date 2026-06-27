"""Converge on the CHAMPION: combine the winning elements (cross-sec / dual momentum x concentration
x weekly cadence x regime), stress-tested across neighbors. Full + both walk-forward halves."""
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
def run(wfn,a,b,every):
    eq=[1.0];wp={}
    for i in range(a+W,b-1):
        if (i-(a+W))%every==0: w=wfn(i)
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in good)
        ret=sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i]>0)
        eq.append(eq[-1]*(1+ret-to*FEE));wp=w
    return eq
def reg(i,m): return P["BTC"][i]>P["BTC"][i-m:i].mean()
def mk(k,lb,regm,dual):
    def wfn(i):
        if not reg(i,regm): return {}
        s=sorted(good,key=lambda c:P[c][i]/P[c][i-lb]-1,reverse=True)
        if dual: s=[c for c in s if P[c][i]>P[c][i-50:i].mean()]   # also require absolute uptrend
        s=s[:k]
        return {c:1.0/k for c in s} if s else {}
    return wfn
def sc(wfn,ev):
    fl=met(run(wfn,0,n,ev)); h1=met(run(wfn,0,n//2,ev)); h2=met(run(wfn,n//2,n,ev))
    return fl,min(h1[1],h2[1])
print(f"{len(good)} coins, {n} bars\n")
print("CHAMPION SEARCH: cross-sec(C)/dual(D) x top-K x lookback x regime x cadence")
print(f"   {'config':<34}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'worstHalf':>11}")
rows=[]
for dual in (False,True):
    for k in (5,8,12):
        for lb in (20,30,60):
            for regm in (50,100):
                for ev,evn in ((7,"wk"),(1,"d")):
                    fl,wh=sc(mk(k,lb,regm,dual),ev)
                    rows.append((wh,fl,f"{'D' if dual else 'C'} top{k} lb{lb} reg{regm} {evn}"))
# show top 12 by worst-half robustness
rows.sort(reverse=True)
for wh,fl,lab in rows[:12]:
    print(f"   {lab:<34}{fl[0]:>+6.0f}%{fl[1]:>8.2f}{fl[2]:>6.0f}%{wh:>11.2f}")
print(f"\n   ...{len(rows)} configs tested. (sorted by worst-half Sharpe = robustness)")
