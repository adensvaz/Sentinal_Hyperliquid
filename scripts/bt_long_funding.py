"""THE test: does funding (signal + carry) turn the honest -18% long backtest into a real edge?
5.5yr Binance daily prices + real historical funding rates (both public, no auth)."""
import sys, copy, time as _t, httpx
sys.path.insert(0,"src")
from sentinel.config import load_config
from sentinel.backtest import simulate
from sentinel.util.concurrency import pmap
COINS=["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
       "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
SPOT="https://data-api.binance.vision"; FUT="https://fapi.binance.com"
def kl(s,end=None):
    p={"symbol":s+"USDT","interval":"1d","limit":1000}
    if end:p["endTime"]=end
    return httpx.get(SPOT+"/api/v3/klines",params=p,timeout=20).json()
def price(c):
    p1=kl(c)
    if not isinstance(p1,list) or not p1:return None
    p2=kl(c,end=int(p1[0][0])-1)
    return c,{int(r[0]):(float(r[4]),float(r[5])) for r in (p2 if isinstance(p2,list) else [])+p1}
def fund(c):
    # paginate funding history (8h rates) back ~5yr, aggregate to daily
    out={}; end=None
    for _ in range(12):
        rows=httpx.get(FUT+"/fapi/v1/fundingRate",params={"symbol":c+"USDT","limit":1000,**({"endTime":end} if end else {})},timeout=20).json()
        if not isinstance(rows,list) or not rows: break
        for r in rows:
            day=int(r["fundingTime"])//86400000*86400000
            out[day]=out.get(day,0.0)+float(r["fundingRate"])
        end=int(rows[0]["fundingTime"])-1
        if len(rows)<1000: break
    return c,out
print("fetching 5.5yr prices + funding from Binance (no auth)...")
ser={c:d for c,d in (r for r in pmap(price,COINS,workers=10) if r)}
fr ={c:d for c,d in (r for r in pmap(fund ,COINS,workers=10) if r)}
ref=max(ser,key=lambda c:len(ser[c])); common=sorted(ser[ref])
closes,vols,fdict,good={},{},{},[]
for c in ser:
    ac,av,have,lc,lv=[],[],0,None,None
    for t in common:
        if t in ser[c]:lc,lv=ser[c][t];have+=1
        ac.append(lc);av.append(lv)
    if have/len(common)>=.9 and ac[0] is not None:
        k="E-"+c+"-USDT"; closes[k]=ac;vols[k]=av;good.append(k)
        fdict[k]=[ (fr.get(c,{}).get(t,0.0)) for t in common ]
T=common; hist={"universe":good,"T":T,"closes":closes,"vols":vols,"interval_h":24.0}
print(f"{len(good)} coins, {len(T)} bars ({_t.strftime('%Y-%m',_t.localtime(T[0]/1000))}->{_t.strftime('%Y-%m',_t.localtime(T[-1]/1000))})\n")
base=load_config(); base.signal.momentum_horizons=["1day","3day","7day","14day"]
fee=0.00025 if base.execution.maker else 0.00075; slip=2.0 if base.execution.maker else base.execution.slippage_bps
def yr(cv):
    b={}
    for ts,eq,_g in cv: y=_t.strftime("%Y",_t.localtime(ts/1000)); b.setdefault(y,[eq,eq])[1]=eq
    return " ".join(f"{y[2:]}:{(v[1]/v[0]-1)*100:+.0f}" for y,(v0,v) in [(y,(vv[0],vv[1])) for y,vv in sorted(b.items())] for v0,v in [(v[0] if False else vv[0],vv[1])])
def yr2(cv):
    b={}
    for ts,eq,_g in cv: y=_t.strftime("%Y",_t.localtime(ts/1000)); b.setdefault(y,[eq,eq])[1]=eq
    return " ".join(f"{y[2:]}:{(v[1]/v[0]-1)*100:+.0f}" for y,v in sorted(b.items()))
print("=== LONG BACKTEST: price-only vs +funding ===")
m0=simulate(hist,copy.deepcopy(base),rebalance_every=1,taker_fee=fee,slippage_bps=slip)
m1=simulate(hist,copy.deepcopy(base),rebalance_every=1,taker_fee=fee,slippage_bps=slip,funding=fdict)
for lab,m in (("price-only (current)",m0),("+ funding signal+carry",m1)):
    print(f"  {lab:<24} ret {m['net_return_pct']:+6.0f}%  Sh {m['sharpe']:5.2f}  DD {m['max_dd_pct']:4.0f}%  fundPnL {m.get('funding_total',0)/100:+.0f}%  | {yr2(m['curve'])}")
