"""CARRY — improvement lab. Test each Sharpe lever independently at REALISTIC fills (6bps + slippage),
keep only what robustly helps (per-year, not one lucky number). No p-hacking — the grid taught us that.

Baseline = the live book: K5, lb21, equal-weight, daily rebalance, funding-tilt.
Levers:
  A. inverse-vol weighting  (stop one volatile name — ACE — from dominating; the #1 Sharpe lever)
  B. more names K           (diversification)
  C. rebalance frequency    (cut turnover/fees)
  D. multi-horizon momentum (7/21/60 blend — a more robust signal)
  E. per-name weight cap     (tail control)
Then the best COMBINATION, with per-year robustness.
"""
import time, json, urllib.request
import numpy as np, datetime as dt
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "UNI","AAVE","FIL","ETC","XLM","INJ","SUI","ARB","OP","APT"]
COST=0.0006; GROSS=1.5   # realistic fills, live gross


def get(u):
    try: return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=25))
    except Exception: return None
def klines(s):
    o={};e=int(time.time()*1000);t=e-1700*86400000
    while t<e:
        k=get(f"https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval=1d&startTime={t}&limit=1500")
        if not k:break
        for r in k:o[int(r[0])//86400000]=float(r[4])
        if len(k)<1500:break
        t=k[-1][0]+1
    return o
def funding(s):
    o={};e=int(time.time()*1000);t=e-1700*86400000
    while t<e:
        f=get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&startTime={t}&limit=1000")
        if not f:break
        for r in f:o[int(r['fundingTime'])//86400000]=o.get(int(r['fundingTime'])//86400000,0.)+float(r['fundingRate'])
        if len(f)<1000:break
        t=f[-1]['fundingTime']+1
    return o
print("fetching...")
P={};F={}
for c in COINS:
    px=klines(c+'USDT');fd=funding(c+'USDT')
    if len(px)>600 and len(fd)>600:P[c]=px;F[c]=fd
days=sorted(set.intersection(*[set(P[c]) for c in P]));coins=list(P);n=len(days)
CL={c:np.array([P[c][d] for d in days]) for c in coins}
FU={c:np.array([F[c].get(d,0.) for d in days]) for c in coins}
RET={c:np.diff(CL[c],prepend=CL[c][0])/CL[c] for c in coins}
yr=np.array([dt.datetime.utcfromtimestamp(d*86400).year for d in days])
print(f"{len(coins)} coins · {n/365:.1f}yr\n")
def z(x):
    x=np.array(x,float);s=x.std();return (x-x.mean())/s if s>0 else x*0

def run(K=5,lbs=(21,),invvol=False,rebal=1,cap=0.0,favg=5,fw=1.0,skip=0):
    eq=[1.0];wp={};rets=[];w={}
    warm=max(max(lbs),favg,21)+skip+1
    for i in range(warm,n-1):
        if (i-warm)%rebal==0:
            mom={c:np.mean([z([CL[k][i-skip]/CL[k][i-lb-skip]-1 for k in coins])[coins.index(c)] for lb in lbs]) for c in coins} if len(lbs)>1 \
                else {c:CL[c][i-skip]/CL[c][i-lbs[0]-skip]-1 for c in coins}
            fnd={c:FU[c][i-favg:i].mean() for c in coins}
            zm=dict(zip(coins,z([mom[c] for c in coins])));zf=dict(zip(coins,z([fnd[c] for c in coins])))
            sc={c:zm[c]-fw*zf[c] for c in coins}
            rank=sorted(coins,key=lambda c:sc[c],reverse=True);longs,shorts=rank[:K],rank[-K:]
            if invvol:
                vol={c:RET[c][max(0,i-21):i].std() or 1e-9 for c in coins}
                lw={c:1/vol[c] for c in longs};sw={c:1/vol[c] for c in shorts}
                sl=sum(lw.values());ss=sum(sw.values())
                w={c:GROSS/2*lw[c]/sl for c in longs}
                for c in shorts:w[c]=w.get(c,0)-GROSS/2*sw[c]/ss
            else:
                w={c:GROSS/2/K for c in longs}
                for c in shorts:w[c]=w.get(c,0)-GROSS/2/K
            if cap>0:
                for c in list(w):w[c]=max(-cap*GROSS,min(cap*GROSS,w[c]))
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in coins)
        pr=sum(w.get(c,0)*(CL[c][i+1]/CL[c][i]-1) for c in coins)
        fp=sum(-np.sign(w.get(c,0))*abs(w.get(c,0))*FU[c][i+1] for c in coins)
        eq.append(eq[-1]*(1+pr+fp-to*COST));wp=dict(w);rets.append(pr+fp-to*COST)
    eq=np.array(eq);rets=np.array(rets)
    cagr=(eq[-1]**(365/len(rets))-1)*100 if eq[-1]>0 else -100
    sh=rets.mean()/rets.std()*np.sqrt(365) if rets.std()>0 else 0
    dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr,sh,dd,rets,list(range(warm,n-1))

base=run()
print(f"{'variant':<34}{'APY':>7}{'Sharpe':>8}{'maxDD':>7}{'ΔSharpe':>9}")
print("-"*66)
def show(name,**kw):
    c,s,d,_,_=run(**kw);print(f"{name:<34}{c:>+6.0f}%{s:>8.2f}{d:>6.0f}%{s-base[1]:>+9.2f}");return s
show("BASELINE (K5 lb21 eq-wt daily)")
print("-- single levers --")
show("A. inverse-vol weighting",invvol=True)
show("B. more names K=8",K=8)
show("B. more names K=10",K=10)
show("C. rebalance every 2d",rebal=2)
show("C. rebalance every 3d",rebal=3)
show("D. multi-horizon mom 7/21/60",lbs=(7,21,60))
show("E. per-name cap 15%",cap=0.15)
print("-- signal-blend levers (the ones that might actually help) --")
show("F. funding weight 0 (pure mom)",fw=0.0)
show("F. funding weight 0.5",fw=0.5)
show("F. funding weight 1.5",fw=1.5)
show("F. funding weight 2.0",fw=2.0)
show("G. lookback 14",lbs=(14,))
show("G. lookback 30",lbs=(30,))
show("H. momentum skip last 1d",skip=1)
show("I. fw1.5 + K5 (best-guess tune)",fw=1.5)
print("-"*66)
# per-year robustness of the best combo
print("\nPER-YEAR robustness (Sharpe) — baseline vs best combo:")
for name,kw in [("baseline",{}),("A+B+C+D",dict(invvol=True,K=8,rebal=2,lbs=(7,21,60)))]:
    c,s,d,rets,idx=run(**kw);yofr=yr[idx[0]:idx[0]+len(rets)]
    row=f"  {name:<12}"
    for y in [2023,2024,2025,2026]:
        m=yofr==y;rr=rets[m];row+=f"{y}:{(rr.mean()/rr.std()*np.sqrt(365) if len(rr)>10 and rr.std()>0 else 0):>5.1f} "
    row+=f" | APY {c:+.0f}%  Sharpe {s:.2f}  DD {d:.0f}%"
    print(row)
print("\nKEEP only levers with +ΔSharpe that also hold across most years. Reject the rest.")
