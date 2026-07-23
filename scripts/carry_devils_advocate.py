"""CARRY — DEVIL'S ADVOCATE. The live book is +7.5% over ~9mo (~+10%/yr) but the dashboard advertises
+123%/yr. That 10x gap says the backtest is optimistic — so assume the edge is fake and try to break it:

  1. DECOMPOSE: is the edge FUNDING (the claimed differentiator) or just MOMENTUM in disguise? Test
     funding-only vs momentum-only vs the combined book. If funding-only is flat, 'funding carry' is a myth.
  2. FEE STRESS: dollar-neutral 2x-gross daily rebalancing churns. Does it survive maker vs taker costs?
  3. DECAY: per-year — has the funding premium compressed as crypto matured (like Neutral's momentum did)?
  4. THE TAIL: raw drawdown + worst month (funding carry = 'pick up pennies in front of a steamroller').
  5. IS IT BETA? net exposure + BTC correlation. Reconcile the annualized number with the live +10%/yr.

Real funding rates (Binance 8h, summed to daily) + real prices. Survivorship-caveated (today's coins).
"""
import sys, time, json, urllib.request
import numpy as np, datetime as dt
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "UNI","AAVE","FIL","ETC","XLM","INJ","SUI","ARB","OP","APT"]


def get(url):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=25))
    except Exception:
        return None


def klines(sym):
    out = {}
    end = int(time.time()*1000); start = end - 1700*24*3600*1000
    s = start
    while s < end:
        k = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1d&startTime={s}&limit=1500")
        if not k: break
        for r in k: out[int(r[0])//86400000] = float(r[4])   # day-index -> close
        if len(k) < 1500: break
        s = k[-1][0] + 1
    return out


def funding(sym):
    out = {}
    end = int(time.time()*1000); s = end - 1700*24*3600*1000
    while s < end:
        f = get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={s}&limit=1000")
        if not f: break
        for r in f:
            d = int(r["fundingTime"])//86400000
            out[d] = out.get(d, 0.0) + float(r["fundingRate"])   # sum the 8h prints into a daily rate
        if len(f) < 1000: break
        s = f[-1]["fundingTime"] + 1
    return out


print("fetching daily prices + REAL funding rates for perps (this takes a moment)...")
P = {}; F = {}
for c in COINS:
    px = klines(c+"USDT"); fd = funding(c+"USDT")
    if len(px) > 600 and len(fd) > 600:
        P[c] = px; F[c] = fd
days = sorted(set.intersection(*[set(P[c]) for c in P]))
coins = list(P); n = len(days)
# matrices aligned on common days
CL = {c: np.array([P[c][d] for d in days]) for c in coins}
FU = {c: np.array([F[c].get(d, 0.0) for d in days]) for c in coins}
yr = np.array([dt.datetime.utcfromtimestamp(d*86400).year for d in days])
print(f"{len(coins)} coins · {n} days ({dt.datetime.utcfromtimestamp(days[0]*86400):%Y-%m} to {dt.datetime.utcfromtimestamp(days[-1]*86400):%Y-%m})\n")


def z(x):
    x = np.array(x, float); m = x.mean(); s = x.std()
    return (x - m) / s if s > 0 else x * 0


def backtest(mode, K=5, lb=21, favg=5, fee=0.0002, gross=1.0):
    """mode: 'funding' (short hi-funding/long lo), 'momentum' (long winners/short losers), 'combined'.
    Daily rebalance, dollar-neutral top-K/bottom-K. PnL = price + funding_collected - fees."""
    eq = [1.0]; wp = {}; rets = []; nets = []
    for i in range(max(lb, favg) + 1, n - 1):
        mom = {c: CL[c][i]/CL[c][i-lb]-1 for c in coins}
        fnd = {c: FU[c][i-favg:i].mean() for c in coins}         # trailing-avg funding
        if mode == "funding":   sc = {c: -fnd[c] for c in coins}                # long low-funding, short high
        elif mode == "momentum": sc = {c: mom[c] for c in coins}                # long winners, short losers
        else:                                                                   # combined (the real book)
            zm = dict(zip(coins, z([mom[c] for c in coins]))); zf = dict(zip(coins, z([fnd[c] for c in coins])))
            sc = {c: zm[c] - zf[c] for c in coins}
        rank = sorted(coins, key=lambda c: sc[c], reverse=True)
        longs, shorts = rank[:K], rank[-K:]
        w = {c: gross/2/K for c in longs}
        for c in shorts: w[c] = w.get(c, 0) - gross/2/K
        to = sum(abs(w.get(c,0)-wp.get(c,0)) for c in coins)
        # PnL: price move + funding (short collects positive funding: -sign*funding)
        pr = sum(w.get(c,0)*(CL[c][i+1]/CL[c][i]-1) for c in coins)
        fp = sum(-np.sign(w.get(c,0))*abs(w.get(c,0))*FU[c][i+1] for c in coins)   # funding earned next day
        net = pr + fp - to*fee
        eq.append(eq[-1]*(1+net)); wp = w; rets.append(net); nets.append(sum(w.values()))
    return np.array(eq), np.array(rets), np.array(nets), list(range(max(lb,favg)+1, n-1))


def stats(eq, rets):
    yrs = len(rets)/365
    sh = rets.mean()/rets.std()*np.sqrt(365) if rets.std()>0 else 0
    cagr = (eq[-1]**(1/yrs)-1)*100 if eq[-1]>0 else -100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    pf = rets[rets>0].sum()/abs(rets[rets<0].sum()) if (rets<0).any() else 99
    return cagr, sh, dd, pf


years = [2021,2022,2023,2024,2025,2026]
print("="*80)
print("1. DECOMPOSITION — is the edge FUNDING or just MOMENTUM?  (per-year Sharpe · CAGR · maxDD · PF)")
print("="*80)
print(f"{'variant':<14}"+"".join(f"{y:>6}" for y in years)+f"{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'PF':>6}")
print("-"*80)
res = {}
for mode in ("funding","momentum","combined"):
    eq,rets,nets,idx = backtest(mode); res[mode]=(eq,rets,nets,idx)
    row=f"{mode:<14}"
    yofr=yr[idx[0]:idx[0]+len(rets)]
    for y in years:
        mask=yofr==y; rr=rets[mask]
        row+=f"{(rr.mean()/rr.std()*np.sqrt(365) if len(rr)>10 and rr.std()>0 else 0):>6.1f}"
    c,s,dd,pf=stats(eq,rets); row+=f"{c:>+6.0f}%{s:>8.2f}{dd:>6.0f}%{pf:>6.2f}"
    print(row)
print("\n  -> if FUNDING-only is weak and MOMENTUM-only ~= COMBINED, the 'funding carry' story is a myth.")

print("\n"+"="*80)
print("2. FEE STRESS (combined book, 1x gross, daily rebalance)")
print("="*80)
for lbl,fee in [("maker ~2bps/side",0.0002),("blended ~4bps",0.0004),("taker ~6bps/side",0.0006)]:
    eq,rets,_,_=backtest("combined",fee=fee); c,s,dd,pf=stats(eq,rets)
    print(f"  {lbl:<20} CAGR {c:>+5.0f}%  Sharpe {s:>5.2f}  maxDD {dd:>3.0f}%  PF {pf:.2f}")

print("\n"+"="*80)
print("3. TAIL + BETA + LIVE RECONCILE (combined, taker fees = honest)")
print("="*80)
eq,rets,nets,idx=backtest("combined",fee=0.0006); c,s,dd,pf=stats(eq,rets)
# worst month
mo={}
for k,i in enumerate(idx[:len(rets)]):
    key=dt.datetime.utcfromtimestamp(days[i]*86400).strftime("%Y-%m"); mo.setdefault(key,0.0); mo[key]+=rets[k]
mv=np.array(list(mo.values()))
btc=np.array([CL["BTC"][i+1]/CL["BTC"][i]-1 for i in idx[:len(rets)]])
corr=np.corrcoef(rets,btc)[0,1]
print(f"  CAGR {c:+.0f}%  Sharpe {s:.2f}  maxDD {dd:.0f}%  PF {pf:.2f}  (at 1x gross)")
print(f"  worst month {mv.min()*100:+.1f}%  ·  negative months {100*(mv<0).mean():.0f}%  ·  avg net exposure {nets.mean()*100:+.1f}%")
print(f"  BTC correlation {corr:+.2f}   (near 0 = market-neutral, real diversifier)")
print(f"  LIVE is 1.5x gross -> honest backtest ~{c*1.5:+.0f}%/yr equiv vs advertised +123%/yr vs LIVE ~+10%/yr")
print("\nVERDICT: real if funding-only carries its weight, it survives taker fees, doesn't decay, ~0 BTC corr.")
