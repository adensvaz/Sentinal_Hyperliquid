"""Grid v3 — quant deep-dive. Tests, layer by layer, what actually improves a grid book:
  (a) vol-adaptive spacing (grid width ~ each coin's realized vol)
  (b) Kaufman Efficiency Ratio range filter (grid only in genuine chop; off in trends)
  (c) long+short vs long-only
  (d) REALISTIC fills: maker (0.015%) vs taker (0.045%) stress test
  (e) portfolio Sharpe across many coins (the metric that matters)
"""
import urllib.request, json, math, time
import numpy as np
COINS=["BTCUSDT","ETHUSDT","SOLUSDT","LINKUSDT","AVAXUSDT","DOGEUSDT","ADAUSDT","LTCUSDT","DOTUSDT","NEARUSDT"]
def klines(sym, days=330):
    end=int(time.time()*1000); start=end-days*24*3600*1000; out=[]; s=start
    while s<end:
        u=f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&startTime={s}&limit=1000"
        try: k=json.load(urllib.request.urlopen(u,timeout=20))
        except: break
        if not k: break
        out+=k; s=k[-1][0]+1
        if len(k)<1000: break
    return out
def ema(a,span):
    al=2/(span+1); e=np.empty(len(a)); e[0]=a[0]
    for i in range(1,len(a)): e[i]=al*a[i]+(1-al)*e[i-1]
    return e
def eff_ratio(c,n=48):   # Kaufman: |net move| / total path. ~0 chop, ~1 trend
    er=np.zeros(len(c))
    for i in range(n,len(c)):
        net=abs(c[i]-c[i-n]); path=np.abs(np.diff(c[i-n:i+1])).sum()
        er[i]=net/path if path>0 else 1
    return er

def grid(k, spacing=0.012, K=6, stop=0.10, fee=0.00015, recenter=168,
         vol_spacing=False, er_filter=False, er_thr=0.35, band=0.08, both=False):
    o=np.array([float(x[1]) for x in k]);h=np.array([float(x[2]) for x in k])
    l=np.array([float(x[3]) for x in k]);c=np.array([float(x[4]) for x in k])
    n=len(c); r=np.diff(c,prepend=c[0])/c
    vol=np.array([r[max(0,i-24):i+1].std() for i in range(n)])   # hourly realized vol
    er=eff_ratio(c) if er_filter else np.zeros(n)
    ma=ema(c,100)
    center=c[0]; longs=[]; shorts=[]; realized=0.0; trades=[]; eq=[]; unit=1.0/K
    for i in range(n):
        sp = max(0.004, min(0.04, 2.0*vol[i])) if vol_spacing else spacing
        if i%recenter==0 and not longs and not shorts: center=c[i]
        ranging = (er[i] < er_thr) if er_filter else (abs(c[i]/ma[i]-1) < band)
        # stops
        if longs and l[i] <= center*(1-stop):
            spx=center*(1-stop)
            for bp in longs: rr=((spx/bp-1)-2*fee)*unit; realized+=rr; trades.append(rr)
            longs=[]; center=c[i]
        if shorts and h[i] >= center*(1+stop):
            spx=center*(1+stop)
            for bp in shorts: rr=((bp/spx-1)-2*fee)*unit; realized+=rr; trades.append(rr)
            shorts=[]; center=c[i]
        if ranging:
            while len(longs)<K:
                lvl=center*(1-(len(longs)+1)*sp)
                if l[i]<=lvl: longs.append(lvl)
                else: break
            if both:
                while len(shorts)<K:
                    lvl=center*(1+(len(shorts)+1)*sp)
                    if h[i]>=lvl: shorts.append(lvl)
                    else: break
        longs.sort()
        while longs and h[i]>=longs[0]*(1+sp): bp=longs.pop(0); rr=(sp-2*fee)*unit; realized+=rr; trades.append(rr)
        shorts.sort(reverse=True)
        while shorts and l[i]<=shorts[0]*(1-sp): bp=shorts.pop(0); rr=(sp-2*fee)*unit; realized+=rr; trades.append(rr)
        op=sum((c[i]/bp-1)*unit for bp in longs)+sum((bp/c[i]-1)*unit for bp in shorts)
        eq.append(realized+op)
    return np.array(eq), np.array(trades)

def perf(eq,tr):
    if len(tr)==0: return 0,0,0,0
    dd=np.max(np.maximum.accumulate(eq)-eq)
    hourly=np.diff(eq,prepend=0); sh=hourly.mean()/hourly.std()*math.sqrt(24*365) if hourly.std()>0 else 0
    return eq[-1], dd, (tr>0).mean()*100, sh

print("loading 10 coins hourly...\n")
DATA={s:klines(s) for s in COINS}; DATA={s:k for s,k in DATA.items() if len(k)>2000}
def portfolio(label,**kw):
    eqs=[]; rets=[]; dds=[]; wins=[]
    for s,k in DATA.items():
        eq,tr=grid(k,**kw)
        if len(tr)==0: continue
        r,dd,w,sh=perf(eq,tr); rets.append(r); dds.append(dd); wins.append(w); eqs.append(eq)
    m=min(len(e) for e in eqs); port=np.mean([e[:m] for e in eqs],axis=0)
    _,pdd,_,psh=perf(port,np.array([1]*10))
    print(f"{label:<40} ret {np.mean(rets)*100:>+5.0f}%  DD {np.mean(dds)*100:>4.0f}%  win {np.mean(wins):>3.0f}%  PORT Sharpe {psh:>4.2f} DD {pdd*100:.0f}%")
    return psh

print(f"{'variant':<40}{'avg ret':>8}{'avg DD':>7}{'win':>5}  portfolio metrics")
print("-"*92)
portfolio("v2 baseline (fixed spacing, band filter)", vol_spacing=False, er_filter=False)
portfolio("+ vol-adaptive spacing", vol_spacing=True, er_filter=False)
portfolio("+ efficiency-ratio filter", vol_spacing=False, er_filter=True)
portfolio("v3: vol-spacing + ER filter", vol_spacing=True, er_filter=True)
portfolio("v3 + long&short", vol_spacing=True, er_filter=True, both=True)
print("\n--- REALISTIC FILLS stress test (v3) ---")
portfolio("v3 maker fills (0.015%)", vol_spacing=True, er_filter=True, fee=0.00015)
portfolio("v3 blended fills (0.03%)", vol_spacing=True, er_filter=True, fee=0.0003)
portfolio("v3 TAKER fills (0.045%)", vol_spacing=True, er_filter=True, fee=0.00045)
