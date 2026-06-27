"""Can a regime filter turn the honest -18% long backtest into a real edge? Test on 5.5yr Binance data."""
import sys, copy, time as _t, httpx
sys.path.insert(0,"src")
from sentinel.config import load_config
from sentinel.backtest import simulate
from sentinel.strategy.portfolio import dispersion
from sentinel.util.concurrency import pmap
COINS=["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
       "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
HOST="https://data-api.binance.vision"
def kl(s,end=None):
    p={"symbol":s+"USDT","interval":"1d","limit":1000}
    if end:p["endTime"]=end
    return httpx.get(HOST+"/api/v3/klines",params=p,timeout=20).json()
def fetch(c):
    p1=kl(c)
    if not isinstance(p1,list) or not p1:return None
    p2=kl(c,end=int(p1[0][0])-1)
    return c,{int(r[0]):(float(r[4]),float(r[5])) for r in (p2 if isinstance(p2,list) else [])+p1}
ser={c:d for c,d in (r for r in pmap(fetch,COINS,workers=10) if r)}
ref=max(ser,key=lambda c:len(ser[c])); common=sorted(ser[ref])
closes,vols,good={},{},[]
for c in ser:
    ac,av,have,lc,lv=[],[],0,None,None
    for t in common:
        if t in ser[c]:lc,lv=ser[c][t];have+=1
        ac.append(lc);av.append(lv)
    if have/len(common)>=.9 and ac[0] is not None: closes["E-"+c+"-USDT"]=ac;vols["E-"+c+"-USDT"]=av;good.append("E-"+c+"-USDT")
T=common; hist={"universe":good,"T":T,"closes":closes,"vols":vols,"interval_h":24.0}
print(f"{len(good)} coins, {len(T)} bars ({_t.strftime('%Y-%m',_t.localtime(T[0]/1000))}->{_t.strftime('%Y-%m',_t.localtime(T[-1]/1000))})\n")
base=load_config(); base.signal.momentum_horizons=["1day","3day","7day","14day"]
fee=0.00025 if base.execution.maker else 0.00075; slip=2.0 if base.execution.maker else base.execution.slippage_bps
def yr(cv):
    b={}
    for ts,eq,_g in cv: y=_t.strftime("%Y",_t.localtime(ts/1000)); b.setdefault(y,[eq,eq])[1]=eq
    return {y:(v[1]/v[0]-1)*100 for y,v in sorted(b.items())}
def go(c,lab):
    m=simulate(hist,c,rebalance_every=1,taker_fee=fee,slippage_bps=slip)
    ys=yr(m["curve"]); ystr=" ".join(f"{y[2:]}:{r:+.0f}" for y,r in ys.items())
    print(f"  {lab:<28} ret {m['net_return_pct']:+6.0f}%  Sh {m['sharpe']:5.2f}  DD {m['max_dd_pct']:4.0f}%  | {ystr}")
print("=== LONG-DATA VARIANTS (per-year %) ===")
go(copy.deepcopy(base),"baseline (current)")
for refv in (0.06,0.08,0.10):
    c=copy.deepcopy(base); c.risk.dispersion_gate=True; c.risk.dispersion_ref_abs=refv; c.risk.dispersion_ref_window=20; c.risk.dispersion_min_scale=0.2
    go(c,f"dispersion-gate ref{refv}")
