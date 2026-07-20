"""Backtest the EXACT rule the user asked for: sweep 40% of profits weekly into a
separate vault (for withdraw / reinvest), per strategy, on real data.

Rule (robust high-water-mark version): every 7 days, if the trading pool is at a NEW high,
move 40% of the new profit into the Vault and reset the mark. Never sweep a down/flat week
(so you don't over-sweep in chop). The Vault is safe cash — withdrawn or reinvested.

Reports per strategy: no-sweep baseline vs 40%-weekly-sweep, showing what's banked (income),
what's still trading, total wealth, and the drawdown on the at-risk pool.
"""
import urllib.request, json, math, numpy as np
rng = np.random.default_rng(5)

COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","AVAXUSDT",
         "DOGEUSDT","LINKUSDT","DOTUSDT","LTCUSDT","ATOMUSDT","NEARUSDT","AAVEUSDT"]
def fetch(s, limit=1500):
    u=f"https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval=1d&limit={limit}"
    try:
        with urllib.request.urlopen(u,timeout=20) as r: return {int(x[0]):float(x[4]) for x in json.load(r)}
    except: return {}
data={c:fetch(c) for c in COINS}; data={c:v for c,v in data.items() if len(v)>400}
common=sorted(set.intersection(*[set(v.keys()) for v in data.values()]))
syms=list(data); px=np.array([[data[s][t] for s in syms] for t in common])
rets=px[1:]/px[:-1]-1; ndays=len(rets); nc=len(syms)
btc=px[:,0]; ma100=np.array([btc[max(0,i-100):i+1].mean() for i in range(len(btc))])
mom30=np.full((len(px),nc),np.nan)
for i in range(30,len(px)): mom30[i]=px[i]/px[i-30]-1
print(f"real Binance data: {nc} coins, {ndays} days (~{ndays/365:.1f}yr)\n")

def voltar(x,t=0.012):
    s=x.std(); return x*(t/s) if s>0 else x

# --- reconstruct each strategy's daily returns ---
def trend():
    d=np.zeros(ndays)
    for i in range(1,ndays):
        m=mom30[i-1]
        if np.isnan(m).any(): continue
        o=np.argsort(m); d[i]=0.5*rets[i,o[-8:]].mean()-0.5*rets[i,o[:8]].mean()
    return voltar(d,0.017)
def champion():
    d=np.zeros(ndays)
    for i in range(1,ndays):
        m=mom30[i-1]
        if np.isnan(m).any(): continue
        o=np.argsort(m); d[i]=rets[i,o[-5:]].mean() if btc[i-1]>ma100[i-1] else 0.0
    return voltar(d,0.016)
# carry + neutral: modelled to documented profiles (real funding reconstruction is heavy)
carry=rng.normal(0.20/365, 0.075/math.sqrt(365), ndays)      # Sharpe ~1.6, ~7.5% vol
neutral=rng.normal(0.06/365, 0.09/math.sqrt(365), ndays)     # thin edge, Sharpe ~0.3

BOOKS={"Trend":trend(),"Champion":champion(),"Carry (modelled)":carry,"Neutral (modelled)":neutral}

def stats(eq_curve):
    pk=np.maximum.accumulate(eq_curve); dd=((pk-eq_curve)/pk).max()
    ann=(eq_curve[-1]/eq_curve[0])**(365/len(eq_curve))-1
    return ann, dd

def run(daily, sweep=0.40, week=7, principal=10000.0):
    trading=principal; vault=0.0; hwm=principal
    tcurve=[]; totcurve=[]
    for i,r in enumerate(daily):
        trading*= (1+r)
        if sweep and (i+1)%week==0 and trading>hwm:
            g=trading-hwm; s=sweep*g; trading-=s; vault+=s; hwm=trading
        tcurve.append(trading); totcurve.append(trading+vault)
    return np.array(tcurve), np.array(totcurve), vault

PRINCIPAL=10000.0
for name,daily in BOOKS.items():
    t0,_,_=run(daily,sweep=0.0)              # baseline: full compound
    _,dd0=stats(t0)
    t1,tot1,vault=run(daily,sweep=0.40)      # 40% weekly sweep
    _,ddtot=stats(tot1)                      # DD on TOTAL wealth (vault protected)
    _,ddtrade=stats(t1)                      # DD on the at-risk trading pool
    trading_end=t1[-1]
    give_up=t0[-1]-tot1[-1]
    print(f"### {name}")
    print(f"  no sweep:  ${t0[-1]:>8,.0f} total   (all still at-risk, maxDD {dd0*100:.0f}%)")
    print(f"  40% weekly: ${tot1[-1]:>7,.0f} total = ${trading_end:,.0f} trading + ${vault:,.0f} BANKED")
    print(f"     banked ${vault:,.0f} safe over {ndays/365:.1f}yr  (~${vault/(ndays/7):.0f}/week avg income)")
    print(f"     total-wealth maxDD {dd0*100:.0f}% -> {ddtot*100:.0f}%   (vault can't be lost)")
    print(f"     cost of sweeping: ${give_up:,.0f} less total wealth ({give_up/PRINCIPAL*100:+.0f}% of capital over {ndays/365:.1f}yr)\n")

print("="*66)
print("Note: Carry/Neutral are modelled to their documented profiles (real")
print("funding history reconstruction is heavy); Trend/Champion use real prices.")
