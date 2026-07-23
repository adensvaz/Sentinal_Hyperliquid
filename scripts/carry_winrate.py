"""CARRY — can we raise the WIN RATE (fewer losing months) without gutting the edge?

Win rate is a dial, not a free lunch. For a cross-sectional book the honest levers are REGIME filters:
Carry bleeds in low-DISPERSION chop (coins moving together = no cross-sectional edge) and in MOMENTUM
CRASHES (market rebounds hard after a selloff -> the losers you're short rocket up). We test scaling
gross down in those regimes and measure BOTH consistency (% positive months, worst month) AND edge
(Sharpe, CAGR). Keep a filter only if it lifts win rate WITHOUT killing return.
"""
import time, json, urllib.request
import numpy as np, datetime as dt
COINS=["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR","UNI","AAVE","FIL","ETC","XLM","INJ","SUI","ARB","OP","APT"]
def get(u):
    try: return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=25))
    except: return None
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
CL={c:np.array([P[c][d] for d in days]) for c in coins};FU={c:np.array([F[c].get(d,0.) for d in days]) for c in coins}
def z(x):
    x=np.array(x,float);s=x.std();return (x-x.mean())/s if s>0 else x*0
COST=0.0006;GROSS=1.5;K=5;lb=21;favg=5;warm=favg+lb+1

def run(disp_filter=False,crash_guard=False,names=K,gross=GROSS):
    dret=[];ddate=[];wp={}
    btc=CL["BTC"]
    for i in range(warm,n-1):
        mom={c:CL[c][i]/CL[c][i-lb]-1 for c in coins};fnd={c:FU[c][i-favg:i].mean() for c in coins}
        zm=dict(zip(coins,z([mom[c] for c in coins])));zf=dict(zip(coins,z([fnd[c] for c in coins])))
        sc={c:zm[c]-zf[c] for c in coins};rank=sorted(coins,key=lambda c:sc[c],reverse=True)
        longs,shorts=rank[:names],rank[-names:]
        g=gross
        if disp_filter:
            disp=np.std([mom[c] for c in coins])
            hist=[np.std([CL[c][j]/CL[c][j-lb]-1 for c in coins]) for j in range(max(warm,i-60),i)]
            med=np.median(hist) if hist else disp
            g*=float(np.clip(disp/med,0.3,1.0))            # low dispersion -> shrink
        if crash_guard:
            r20=btc[i]/btc[i-20]-1; r5=btc[i]/btc[i-5]-1
            if r20<-0.12 and r5>0.05: g*=0.35             # selloff then bounce = momentum-crash risk
        w={c:g/2/names for c in longs}
        for c in shorts:w[c]=w.get(c,0)-g/2/names
        to=sum(abs(w.get(c,0)-wp.get(c,0)) for c in coins)
        pr=sum(w.get(c,0)*(CL[c][i+1]/CL[c][i]-1) for c in coins)
        fp=sum(-np.sign(w.get(c,0))*abs(w.get(c,0))*FU[c][i+1] for c in coins)
        dret.append(pr+fp-to*COST);ddate.append(dt.datetime.utcfromtimestamp(days[i]*86400));wp=w
    dret=np.array(dret)
    # monthly
    mo={}
    for r,d in zip(dret,ddate):
        k=d.strftime("%Y-%m");mo.setdefault(k,[]).append(r)
    mret=np.array([np.prod(1+np.array(v))-1 for v in mo.values()])
    sh=dret.mean()/dret.std()*np.sqrt(365) if dret.std()>0 else 0
    eq=np.cumprod(1+dret);dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    cagr=(eq[-1]**(365/len(dret))-1)*100 if eq[-1]>0 else -100
    return dict(cagr=cagr,sh=sh,dd=dd,winmo=(mret>0).mean()*100,worst=mret.min()*100,
                pos_days=(dret>0).mean()*100)

print(f"\n{'config':<30}{'%pos months':>12}{'worst mo':>10}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>7}")
print("-"*76)
def show(name,**kw):
    m=run(**kw);print(f"{name:<30}{m['winmo']:>11.0f}%{m['worst']:>+9.1f}%{m['cagr']:>+7.0f}%{m['sh']:>8.2f}{m['dd']:>6.0f}%")
show("BASELINE")
show("+ dispersion filter",disp_filter=True)
show("+ momentum-crash guard",crash_guard=True)
show("+ BOTH filters",disp_filter=True,crash_guard=True)
show("+ BOTH + more names K8",disp_filter=True,crash_guard=True,names=8)
print("-"*76)
print("READ: a filter EARNS its place only if %positive-months rises AND worst-month shrinks AND")
print("CAGR/Sharpe don't collapse. Higher monthly win-rate at the cost of half the return is a bad trade.")
