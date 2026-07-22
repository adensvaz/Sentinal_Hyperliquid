"""Grid / market-making backtest — the honest version of the on-chain trader's edge.

DESIGN (long-only grid, the safe version):
  - A ladder of BUY levels below a slow center price, spaced `spacing` apart.
  - Buy 1 unit each time price dips to the next level (max `max_levels` deep = position cap).
  - Sell each unit one grid-step up for a `spacing` profit (mean-reversion capture).
  - HARD STOP: if price breaks `stop` below center, close everything (avoids the martingale
    steamroller that blows up naive grids). Re-center when flat.
  - Fees on every fill (maker, since grid orders are post-only limits).
  - Fills use each bar's HIGH/LOW (realistic for resting limit orders).

Tests across several liquid coins + splits performance by regime (ranging vs trending) to show
WHERE it makes money and where it bleeds — the whole point of judging a grid honestly.
"""
import urllib.request, json, math, time
import numpy as np

COINS=["BTCUSDT","ETHUSDT","SOLUSDT","LINKUSDT","AVAXUSDT","DOGEUSDT"]
def klines(sym, interval="1h", days=330):
    end=int(time.time()*1000); start=end-days*24*3600*1000; out=[]; s=start
    while s<end:
        u=f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&startTime={s}&limit=1000"
        try: k=json.load(urllib.request.urlopen(u,timeout=20))
        except: break
        if not k: break
        out+=k; s=k[-1][0]+1
        if len(k)<1000: break
    return out

def grid(o,h,l,c, spacing=0.015, max_levels=6, stop=0.12, fee=0.00015, recenter=168):
    """returns (realized_return_units, trades list, equity curve, exposure series)."""
    center=c[0]; filled=[]  # list of buy prices currently held
    realized=0.0; trades=[]; eq=[]; expo=[]
    for i in range(len(c)):
        # recenter only when flat
        if i%recenter==0 and not filled: center=c[i]
        # STOP first (use the bar low)
        if filled and l[i] <= center*(1-stop):
            sp=center*(1-stop)
            for bp in filled:
                r=(sp/bp-1)-2*fee; realized+=r; trades.append(r)
            filled=[]; center=c[i]
            eq.append(realized); expo.append(0); continue
        # BUY fills: any open level whose price the bar's LOW reached
        while len(filled)<max_levels:
            lvl=center*(1-(len(filled)+1)*spacing)
            if l[i]<=lvl: filled.append(lvl)
            else: break
        # SELL fills: close units whose +spacing target the bar's HIGH reached (close cheapest first)
        filled.sort()
        while filled and h[i]>=filled[0]*(1+spacing):
            bp=filled.pop(0); r=spacing-2*fee; realized+=r; trades.append(r)
        open_pnl=sum(c[i]/bp-1 for bp in filled)
        eq.append(realized+open_pnl); expo.append(len(filled))
    return realized, np.array(trades), np.array(eq), np.array(expo)

def trend_strength(c, win=168):
    # |return over the window| — high = trending, low = ranging
    ts=np.zeros(len(c))
    for i in range(win,len(c)): ts[i]=abs(c[i]/c[i-win]-1)
    return ts

print("fetching hourly data + running the grid per coin...\n")
print(f"{'coin':<9}{'grid PnL':>9}{'trades':>8}{'win%':>6}{'maxDD':>7}{'avg/trade':>10}")
print("-"*52)
all_eq=[]; tot_pnl=0; rang_pnl=0; trend_pnl=0
for sym in COINS:
    k=klines(sym)
    if len(k)<2000: print(f"{sym:<9} (insufficient data)"); continue
    o=np.array([float(x[1]) for x in k]); h=np.array([float(x[2]) for x in k])
    l=np.array([float(x[3]) for x in k]); c=np.array([float(x[4]) for x in k])
    real,tr,eq,expo=grid(o,h,l,c)
    if len(tr)==0: continue
    win=(tr>0).mean()*100
    dd=np.max(np.maximum.accumulate(eq)-eq) if len(eq) else 0
    # regime split: grid PnL earned in ranging vs trending hours
    ts=trend_strength(c); med=np.median(ts[ts>0])
    deq=np.diff(eq, prepend=0)
    rang=deq[ts<=med].sum(); trnd=deq[ts>med].sum()
    rang_pnl+=rang; trend_pnl+=trnd; tot_pnl+=real
    all_eq.append(eq/ max(1,len(COINS)))
    print(f"{sym[:-4]:<9}{real*100:>+8.1f}%{len(tr):>8}{win:>5.0f}%{dd*100:>6.1f}%{tr.mean()*100:>+9.3f}%")

print("-"*52)
print(f"\nPORTFOLIO (equal-weight the coins):")
print(f"  total grid return (sum of per-coin, ~1yr): {tot_pnl/len(COINS)*100:+.1f}% avg per coin")
print(f"\nWHERE the money comes from (regime split):")
print(f"  in RANGING hours:  {rang_pnl/len(COINS)*100:+.1f}%  <- grids love chop")
print(f"  in TRENDING hours: {trend_pnl/len(COINS)*100:+.1f}%  <- grids bleed in trends")
print(f"\n  => a grid is a bet on RANGING. Net {'POSITIVE' if tot_pnl>0 else 'NEGATIVE'} over this ~1yr sample.")

# parameter sensitivity on BTC
print("\n--- parameter sweep (BTC): spacing x stop ---")
k=klines("BTCUSDT"); o=np.array([float(x[1]) for x in k]);h=np.array([float(x[2]) for x in k]);l=np.array([float(x[3]) for x in k]);c=np.array([float(x[4]) for x in k])
print(f"  {'spacing':>8}{'stop':>7}{'PnL':>8}{'trades':>8}{'win%':>6}{'maxDD':>7}")
for sp in [0.008,0.015,0.025]:
    for stp in [0.08,0.12,0.20]:
        real,tr,eq,_=grid(o,h,l,c,spacing=sp,stop=stp)
        if len(tr)==0: continue
        dd=np.max(np.maximum.accumulate(eq)-eq)
        print(f"  {sp*100:>6.1f}%{stp*100:>6.0f}%{real*100:>+7.1f}%{len(tr):>8}{(tr>0).mean()*100:>5.0f}%{dd*100:>6.1f}%")
