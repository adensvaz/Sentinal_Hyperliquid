"""DEFINITIVE Hyperliquid backtest — max available history, HL Funding-Alpha strategy WITH a
funding-dispersion regime filter (size up when the cross-sectional funding spread is wide, down when
compressed — the principled fix for the out-of-sample fade). Reports full + split-half + rolling
quarterly Sharpe so we SEE the robustness, not just a headline."""
import json, os, sys, time as _t
import httpx, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")

INFO = "https://api.hyperliquid.xyz/info"; DAY = 86400000; LOOKBACK = 1000
CACHE = "/tmp/hl_data.json"
COINS = ["BTC","ETH","SOL","BNB","XRP","DOGE","AVAX","LINK","LTC","BCH","ATOM","NEAR","ARB","OP","APT",
         "SUI","INJ","TIA","SEI","DYDX","LDO","AAVE","UNI","CRV","WLD","GALA","PYTH","JTO","STX","WIF",
         "ENA","JUP","PENDLE","ONDO","FIL","TAO"]

def _post(body, tries=6):
    for k in range(tries):
        try:
            r = httpx.post(INFO, json=body, timeout=25)
            if r.status_code == 429: _t.sleep(0.5*(k+1)); continue
            r.raise_for_status(); return r.json()
        except Exception: _t.sleep(0.4*(k+1))
    return None

def load(coin):
    end = int(_t.time()*1000); s0 = end - LOOKBACK*DAY
    cs = _post({"type":"candleSnapshot","req":{"coin":coin,"interval":"1d","startTime":s0,"endTime":end}}) or []
    px = {int(r["t"])//DAY: float(r["c"]) for r in cs if r.get("c")}
    fn, start = {}, s0
    for _ in range(70):
        rows = _post({"type":"fundingHistory","coin":coin,"startTime":start,"endTime":end})
        if not rows: break
        for r in rows: fn[int(r["time"])//DAY] = fn.get(int(r["time"])//DAY,0.0)+float(r["fundingRate"])
        last = int(rows[-1]["time"])
        if last <= start or len(rows) < 500: break
        start = last + 3600000; _t.sleep(0.04)
    return px, fn

if os.path.exists(CACHE):
    raw = json.load(open(CACHE))
    PX = {c:{int(k):v for k,v in d.items()} for c,d in raw["px"].items()}
    FN = {c:{int(k):v for k,v in d.items()} for c,d in raw["fn"].items()}
    print("loaded cache", flush=True)
else:
    PX, FN = {}, {}
    for c in COINS:
        px, fn = load(c)
        if len(px) > 120 and len(fn) > 120: PX[c], FN[c] = px, fn
        print(f"  {c:<7} c={len(px):>4} f={len(fn):>4}{'' if c in PX else ' skip'}", flush=True)
    json.dump({"px":{c:{str(k):v for k,v in d.items()} for c,d in PX.items()},
               "fn":{c:{str(k):v for k,v in d.items()} for c,d in FN.items()}}, open(CACHE,"w"))

coins = list(PX); days = sorted({d for c in coins for d in PX[c]}); n = len(days)
C = {c: np.array([PX[c].get(d, np.nan) for d in days]) for c in coins}
F = {c: np.array([FN[c].get(d, np.nan) for d in days]) for c in coins}
Rr = {c: np.concatenate([[np.nan], C[c][1:]/C[c][:-1]-1]) for c in coins}
d0 = datetime.fromtimestamp(days[0]*DAY/1000, tz=timezone.utc).date()
d1 = datetime.fromtimestamp(days[-1]*DAY/1000, tz=timezone.utc).date()
print(f"\nHyperliquid own data · {len(coins)} coins · {d0}->{d1} ({n/365:.2f}yr)\n", flush=True)

FEE, MOMLB, VOLLB, BETALB, DISPLB = 0.00015, 21, 20, 30, 60
def avgF(c, i, look=5):
    a = F[c][max(0,i-look+1):i+1]; a = a[~np.isnan(a)]
    return a.mean() if len(a) else np.nan
def zof(d):
    v = np.array(list(d.values()))
    if len(v)==0 or v.std()<1e-12: return {k:0.0 for k in d}
    z=(v-v.mean())/v.std(); return {k:z[i] for i,k in enumerate(d)}
def betas(i):
    b=Rr["BTC"][max(0,i-BETALB+1):i+1]; out={}
    for c in coins:
        r=Rr[c][max(0,i-BETALB+1):i+1]; m=~(np.isnan(r)|np.isnan(b))
        if m.sum()>=10 and b[m].std()>1e-9: out[c]=np.cov(r[m],b[m])[0,1]/np.var(b[m])
    return out

def run(alpha=0.9, guard=0.12, K=7, vol_target=0.004, ddthrottle=True, betahedge=True,
        fundweight=True, regime=False):
    eq,peak,rets,rv,wp,disp_hist = 1.0,1.0,[],[],{c:0.0 for c in coins},[]
    for i in range(max(MOMLB,BETALB), n-1):
        elig=[c for c in coins if not np.isnan(C[c][i]) and not np.isnan(C[c][i+1]) and not np.isnan(C[c][i-MOMLB]) and C[c][i-MOMLB]>0 and not np.isnan(avgF(c,i))]
        w={c:0.0 for c in coins}
        disp=np.nan
        if len(elig)>=2*K:
            fnd={c:avgF(c,i) for c in elig}; mom={c:C[c][i]/C[c][i-MOMLB]-1 for c in elig}
            disp=np.std(list(fnd.values()))
            fz,mz=zof(fnd),zof(mom)
            score={c:-alpha*fz[c]+(1-alpha)*mz[c] for c in elig}
            rank=sorted(elig,key=lambda c:score[c]); longs,shorts=rank[-K:],rank[:K]
            longs=[c for c in longs if mom[c]>-guard]; shorts=[c for c in shorts if mom[c]<guard]
            def side(names,sign):
                if not names: return
                ww={c:(abs(fnd[c]) if fundweight else 1.0) for c in names}; s=sum(ww.values()) or 1
                for c in names: w[c]=sign*min(ww[c]/s,0.5)
                s2=sum(abs(w[c]) for c in names) or 1
                for c in names: w[c]=w[c]/s2
            side(longs,+1); side(shorts,-1)
            if betahedge:
                bt=betas(i); nb=sum(w[c]*bt.get(c,0) for c in coins); w["BTC"]=w.get("BTC",0)-nb
        disp_hist.append(disp)
        g=1.0
        if vol_target and len(rv)>=5:
            rvol=np.std(rv[-VOLLB:]); g=min(1.5, vol_target/rvol) if rvol>1e-9 else 1.0
        if regime and not np.isnan(disp):
            base=np.nanmedian([d for d in disp_hist[-DISPLB:] if not np.isnan(d)]) if len([d for d in disp_hist if not np.isnan(d)])>=10 else disp
            g *= float(np.clip(disp/base, 0.4, 1.3)) if base>1e-12 else 1.0
        if ddthrottle:
            cur=1-eq/peak
            if cur>0.04: g*=max(0.30,1-(cur-0.04)/0.08*0.70)
        to=sum(abs(g*w[c]-wp[c]) for c in coins)
        pr=sum(g*w[c]*(C[c][i+1]/C[c][i]-1) for c in coins if w[c])
        fn=sum(-g*w[c]*F[c][i+1] for c in coins if w[c] and not np.isnan(F[c][i+1]))
        r=pr+fn-to*FEE; eq*=(1+r); peak=max(peak,eq); rets.append(r); rv.append(r); wp={c:g*w[c] for c in coins}
    return np.array(rets), eq

def stats(rets, eq):
    yrs=len(rets)/365.0; e=np.cumprod(1+rets); dd=(np.maximum.accumulate(e)-e)/np.maximum.accumulate(e)
    down=rets[rets<0]
    return dict(cagr=eq**(1/yrs)-1, sharpe=rets.mean()/rets.std()*np.sqrt(365) if rets.std()>0 else 0,
                sortino=rets.mean()/down.std()*np.sqrt(365) if len(down)>1 and down.std()>0 else 0,
                maxdd=dd.max(), calmar=(eq**(1/yrs)-1)/dd.max() if dd.max()>1e-6 else 0)

print(f"{'variant':<26}{'CAGR':>8}{'Sharpe':>7}{'Sortino':>8}{'maxDD':>7}{'Calmar':>7}")
for label, rg in [("Funding-Alpha (no regime)", False), ("Funding-Alpha + REGIME filter", True)]:
    rets, eq = run(regime=rg); s = stats(rets, eq)
    print(f"{label:<26}{s['cagr']*100:>+7.1f}%{s['sharpe']:>7.2f}{s['sortino']:>8.2f}{s['maxdd']*100:>7.1f}%{s['calmar']:>7.2f}")
# robustness of the regime-filtered version
rets, eq = run(regime=True)
q = len(rets)//4
sh = lambda x: x.mean()/x.std()*np.sqrt(365) if len(x)>5 and x.std()>0 else 0
print("\nrolling-quarter Sharpe (regime-filtered):", [round(sh(rets[i*q:(i+1)*q]),2) for i in range(4)])
h=len(rets)//2
print(f"split-half Sharpe: 1st {sh(rets[:h]):.2f} | 2nd {sh(rets[h:]):.2f}")
