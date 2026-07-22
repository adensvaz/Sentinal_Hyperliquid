"""Grid deep-dive #2: does the edge survive across REGIMES (bull/bear/chop) and REALISTIC fills?

The 1-yr Sharpe (~2.7 long-only, ~4.9 long+short) came from choppy-up 2024-25 — grid heaven.
Here we pull ~4.5yr hourly and test the SAME grid year-by-year, with a conservative fill model
(blended fee + slippage + require price to trade THROUGH a level, not just touch it).
"""
import urllib.request, json, math, time
import numpy as np, datetime as dt
COINS=["BTCUSDT","ETHUSDT","SOLUSDT","DOGEUSDT","LINKUSDT"]
def klines(sym, days=1650):
    end=int(time.time()*1000); start=end-days*24*3600*1000; out=[]; s=start
    while s<end:
        u=f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&startTime={s}&limit=1000"
        try: k=json.load(urllib.request.urlopen(u,timeout=20))
        except: break
        if not k: break
        out+=k; s=k[-1][0]+1
        if len(k)<1000: break
    return out

def grid(o,h,l,c,ts, spacing=0.012,K=6,stop=0.10,fee=0.0003,slip=0.0002,recenter=168,both=False):
    """conservative fills: a level fills only if the bar traded THROUGH it (low<lvl or high>lvl),
       and every fill pays fee+slip. Each unit = 1/K (1x total, no leverage)."""
    n=len(c); center=c[0]; longs=[]; shorts=[]; realized=0.0; unit=1.0/K
    daily=np.zeros(n); prev_eq=0.0; ma=np.array([c[max(0,i-100):i+1].mean() for i in range(n)])
    for i in range(n):
        if i%recenter==0 and not longs and not shorts: center=c[i]
        ranging = abs(c[i]/ma[i]-1) < 0.08
        cost=fee+slip
        if longs and l[i] <= center*(1-stop):
            spx=center*(1-stop)
            for bp in longs: realized+=((spx/bp-1)-2*cost)*unit
            longs=[]; center=c[i]
        if shorts and h[i] >= center*(1+stop):
            spx=center*(1+stop)
            for bp in shorts: realized+=((bp/spx-1)-2*cost)*unit
            shorts=[]; center=c[i]
        if ranging:
            while len(longs)<K:
                lvl=center*(1-(len(longs)+1)*spacing)
                if l[i]<lvl: longs.append(lvl)      # must trade THROUGH (strict <)
                else: break
            if both:
                while len(shorts)<K:
                    lvl=center*(1+(len(shorts)+1)*spacing)
                    if h[i]>lvl: shorts.append(lvl)
                    else: break
        longs.sort()
        while longs and h[i]>longs[0]*(1+spacing): bp=longs.pop(0); realized+=(spacing-2*cost)*unit
        shorts.sort(reverse=True)
        while shorts and l[i]<shorts[0]*(1-spacing): bp=shorts.pop(0); realized+=(spacing-2*cost)*unit
        eq=realized+sum((c[i]/bp-1)*unit for bp in longs)+sum((bp/c[i]-1)*unit for bp in shorts)
        daily[i]=eq-prev_eq; prev_eq=eq
    return daily

def sharpe(d):
    d=d[d!=0] if (d!=0).any() else d
    return d.mean()/d.std()*math.sqrt(24*365) if len(d)>10 and d.std()>0 else 0

print("loading ~4.5yr hourly (this takes a moment)...\n")
DATA={}
for s in COINS:
    k=klines(s)
    if len(k)>10000: DATA[s]=k
    print(f"  {s}: {len(k)} bars ({dt.datetime.utcfromtimestamp(k[0][0]/1000):%Y-%m} to {dt.datetime.utcfromtimestamp(k[-1][0]/1000):%Y-%m})")

def yearly(both):
    print(f"\n{'='*74}\n{'LONG+SHORT' if both else 'LONG-ONLY'} grid — Sharpe by YEAR (conservative fills)")
    print(f"{'='*74}")
    years=[2021,2022,2023,2024,2025]
    hdr="coin      "+"".join(f"{y:>9}" for y in years)+"   regime note"
    print(hdr); print("-"*74)
    port_by_year={y:[] for y in years}
    for s,k in DATA.items():
        o=np.array([float(x[1]) for x in k]);h=np.array([float(x[2]) for x in k])
        l=np.array([float(x[3]) for x in k]);c=np.array([float(x[4]) for x in k])
        yr=np.array([dt.datetime.utcfromtimestamp(x[0]/1000).year for x in k])
        d=grid(o,h,l,c,None,both=both)
        row=f"{s[:-4]:<10}"
        for y in years:
            m=yr==y
            if m.sum()<500: row+=f"{'—':>9}"; continue
            sh=sharpe(d[m]); port_by_year[y].append(sh); row+=f"{sh:>9.2f}"
        print(row)
    print("-"*74)
    prow="PORTFOLIO "
    for y in years:
        prow+=f"{np.mean(port_by_year[y]):>9.2f}" if port_by_year[y] else f"{'—':>9}"
    print(prow+"   <- avg across coins")
    # regime label per year (BTC net move / path)
    o=np.array([float(x[1]) for x in DATA['BTCUSDT']]);c=np.array([float(x[4]) for x in DATA['BTCUSDT']])
    yr=np.array([dt.datetime.utcfromtimestamp(x[0]/1000).year for x in DATA['BTCUSDT']])
    print("\nBTC regime each year (net return / how trendy):")
    for y in years:
        m=yr==y
        if m.sum()<500: continue
        cc=c[m]; net=cc[-1]/cc[0]-1; path=np.abs(np.diff(cc)).sum()/cc[0]; eff=abs(net)/path if path>0 else 0
        lab="STRONG TREND" if eff>0.10 else ("choppy/ranging" if eff<0.05 else "mixed")
        print(f"  {y}: BTC {net*100:>+5.0f}%  efficiency {eff:.3f}  -> {lab}")

yearly(both=False)
yearly(both=True)
