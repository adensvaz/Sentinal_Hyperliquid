"""Hunt for a REAL, robust edge on 5.5yr Binance data. Hypothesis-driven (trend/regime), benchmarked
vs buy-hold BTC, validated walk-forward (1st half vs 2nd half). No leverage (apples-to-apples)."""
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
print("loading 5.5yr Binance daily closes...")
ser={c:d for c,d in (r for r in pmap(fetch,COINS,workers=10) if r)}
ref=max(ser,key=lambda c:len(ser[c])); T=sorted(ser[ref])
P={}; good=[]
for c in ser:
    a,have,lc=[],0,None
    for t in T:
        if t in ser[c]: lc=ser[c][t];have+=1
        a.append(lc)
    if have/len(T)>=.9 and a[0] is not None: P[c]=np.array(a,float);good.append(c)
n=len(T); FEE=0.0004
print(f"{len(good)} coins, {n} bars ({_t.strftime('%Y-%m',_t.localtime(T[0]/1000))}->{_t.strftime('%Y-%m',_t.localtime(T[-1]/1000))})\n")

def metrics(eq):
    eq=np.array(eq); r=eq[1:]/eq[:-1]-1
    cagr=(eq[-1]/eq[0])**(365/len(eq))-1
    sh=(r.mean()/r.std()*np.sqrt(365)) if r.std()>0 else 0
    peak=np.maximum.accumulate(eq); dd=((peak-eq)/peak).max()*100
    return cagr*100, sh, dd

def run(weight_fn, a=0, b=None):
    b=b or n; eq=[1.0]; w_prev={}
    for i in range(a+60,b-1):
        w=weight_fn(i)                       # target weights dict {coin: signed weight}, |sum|<=1
        to=sum(abs(w.get(c,0)-w_prev.get(c,0)) for c in good)
        ret=sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i]>0)
        eq.append(eq[-1]*(1+ret-to*FEE)); w_prev=w
    return eq

# --- benchmarks ---
def bh_btc(i): return {"BTC":1.0}
def bh_ew(i):  return {c:1.0/len(good) for c in good}
# --- trend signals ---
def above_ma(c,i,w): return P[c][i] > P[c][i-w:i].mean()
def tsmom_long(i):   # long coins above 50d MA, equal weight, else cash (max 1x)
    up=[c for c in good if above_ma(c,i,50)]; 
    return {c:1.0/len(good) for c in up} if up else {}
def tsmom_ls(i):     # long uptrend / short downtrend, dollar-balanced
    up=[c for c in good if above_ma(c,i,50)]; dn=[c for c in good if not above_ma(c,i,50)]
    w={}; 
    for c in up: w[c]=0.5/max(len(up),1)
    for c in dn: w[c]=-0.5/max(len(dn),1)
    return w
def regime_on(i): return P["BTC"][i] > P["BTC"][i-100:i].mean()   # BTC above 100d MA = risk-on
def trend_regime(i):  # long-only trend, but only invest when BTC regime is risk-on
    if not regime_on(i): return {}
    up=[c for c in good if above_ma(c,i,50)]
    return {c:1.0/len(good) for c in up} if up else {}
def trend_regime_volscaled(i):
    if not regime_on(i): return {}
    up=[c for c in good if above_ma(c,i,50)]
    if not up: return {}
    iv={c:1.0/max(P[c][i-20:i].std()/P[c][i-20:i].mean(),1e-3) for c in up}
    s=sum(iv.values()); 
    return {c:min(iv[c]/s,0.20) for c in up}   # inverse-vol, capped 20%/name

STRATS=[("BUY-HOLD BTC",bh_btc),("BUY-HOLD basket",bh_ew),("TSMOM long-only",tsmom_long),
        ("TSMOM long/short",tsmom_ls),("Trend+regime filter",trend_regime),("Trend+regime+volscale",trend_regime_volscaled)]
def show(label,fn):
    f=metrics(run(fn,0,n)); h1=metrics(run(fn,0,n//2)); h2=metrics(run(fn,n//2,n))
    print(f"  {label:<24} FULL: CAGR {f[0]:+6.0f}% Sh {f[1]:5.2f} DD {f[2]:3.0f}%   | H1 Sh {h1[1]:5.2f}  H2 Sh {h2[1]:5.2f}")
print(f"{'strategy':<26}{'    full 5.5yr':<34} walk-forward(both halves)")
print("-"*88)
for lab,fn in STRATS: show(lab,fn)
