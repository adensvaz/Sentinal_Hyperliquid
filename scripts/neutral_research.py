"""Does the MARKET-NEUTRAL book have ANY real edge on 5.5yr clean data — and are we running the
wrong signal sign? Tests dollar-neutral long/short cross-sectional strategies:
  - MOMENTUM (long recent winners / short losers)  vs  REVERSAL (long losers / short winners)
  - lookback 3/7/14/30d, rebalance every 1/3/7d, long-short vs long-only
Realistic fees (turnover * bps). Reports Sharpe / worst-half / CAGR / maxDD / turnover.
Cross-sectional daily momentum is theoretically weak in crypto; short-horizon reversal is the
candidate market-neutral edge. This finds out which (if any) actually pays after fees."""
import sys, httpx, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
def kl(s, end=None):
    p = {"symbol": s+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=20).json()
def f(c):
    p1 = kl(c)
    if not isinstance(p1, list) or not p1: return None
    p2 = kl(c, end=int(p1[0][0])-1)
    return c, {int(r[0]): float(r[4]) for r in (p2 if isinstance(p2, list) else [])+p1}
ser = {c: d for c, d in (r for r in pmap(f, COINS, workers=10) if r)}
ref = max(ser, key=lambda c: len(ser[c])); T = sorted(ser[ref]); P = {}; good = []
for c in ser:
    a, have, lc = [], 0, None
    for t in T:
        if t in ser[c]: lc = ser[c][t]; have += 1
        a.append(lc)
    if have/len(T) >= .9 and a[0] is not None: P[c] = np.array(a, float); good.append(c)
n = len(T); W = 70; YRS = n/365.0; K = 5

def met(eq):
    eq = np.array(eq); r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = ((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))

def mom(c, i, lb): return P[c][i]/P[c][i-lb]-1 if P[c][i-lb] > 0 else -9

def run(sign, lb, ev, longshort, fee):
    """sign=+1 momentum (long winners), -1 reversal (long losers). longshort=True dollar-neutral."""
    eq = [1.0]; wp = {}; to_sum = 0.0; bars = 0
    for i in range(W, n-1):
        if (i-W) % ev == 0:
            rank = sorted(good, key=lambda c: sign*mom(c, i, lb), reverse=True)
            longs, shorts = rank[:K], rank[-K:]
            w = {c: 1.0/K for c in longs}
            if longshort:
                for c in shorts: w[c] = w.get(c, 0) - 1.0/K
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good)
        ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)
        eq.append(eq[-1]*(1+ret-to*fee)); wp = w; to_sum += to; bars += 1
    return eq, to_sum/YRS

FEE = 0.0004
print(f"{len(good)} coins · {YRS:.1f}yr · fee {FEE*1e4:.0f}bps/turn · K={K} per side\n")
print(f"{'strategy':<40}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>7}{'wHalf':>7}{'turn/yr':>9}")
print("-"*78)
for sign, name in [(+1, "MOMENTUM (long winners/short losers)"), (-1, "REVERSAL (long losers/short winners)")]:
    print(f"\n-- {name} · dollar-neutral long/short --")
    for lb in (3, 7, 14, 30):
        for ev in (1, 3, 7):
            eq, turn = run(sign, lb, ev, True, FEE)
            c, sh, dd, wh = met(eq)
            print(f"  lb{lb:<3} rebal{ev}d{'':<26}"[:40]+f"{c:>+6.0f}%{sh:>8.2f}{dd:>6.0f}%{wh:>7.2f}{turn:>8.1f}x")
print("\n-- LONG-ONLY top-K momentum (NOT neutral — shows where the beta edge is) --")
for lb in (14, 30):
    eq, turn = run(+1, lb, 3, False, FEE)
    c, sh, dd, wh = met(eq)
    print(f"  lb{lb} rebal3d long-only{'':<18}"[:40]+f"{c:>+6.0f}%{sh:>8.2f}{dd:>6.0f}%{wh:>7.2f}{turn:>8.1f}x")
print("-"*78)
print("Read: Sharpe>0.5 with wHalf>0.3 = a real, robust market-neutral edge worth pursuing.")
print("If REVERSAL beats MOMENTUM, we've been running the wrong sign for the neutral book.")
