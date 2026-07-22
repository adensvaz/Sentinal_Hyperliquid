"""TREND vs NEUTRAL — which book earns its slot? A fair, parameter-swept, regime-split fight on the
SAME 5.5yr daily history, SAME dollar-neutral long/short construction, SAME realistic turnover fees.

  NEUTRAL  = cross-sectional momentum (long the coins beating the pack, short the laggards).
  TREND    = time-series momentum   (long coins most above their OWN moving average, short most below).

We don't trust one noisy live-paper datapoint (Trend's loss was ~1 trade). Instead: sweep each book's
key params, report robustness (mean/min Sharpe across the grid — overfit books look great at one setting
and fall apart across the grid), split Sharpe by YEAR (regime), and measure the tail (worst day + skew —
Trend's short-squeeze weakness). The more robust, better-tailed book stays; the other is the kill.
"""
import sys, httpx, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap

COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]

def kl(s, end=None):
    p = {"symbol": s+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=20).json()

def fetch(c):
    p1 = kl(c)
    if not isinstance(p1, list) or not p1: return None
    p2 = kl(c, end=int(p1[0][0])-1)
    return c, {int(r[0]): float(r[4]) for r in (p2 if isinstance(p2, list) else [])+p1}

print("loading 5.5yr daily history for 28 coins...")
ser = {c: d for c, d in (r for r in pmap(fetch, COINS, workers=10) if r)}
ref = max(ser, key=lambda c: len(ser[c])); T = sorted(ser[ref]); P = {}; good = []
for c in ser:
    a, have, lc = [], 0, None
    for t in T:
        if t in ser[c]: lc = ser[c][t]; have += 1
        a.append(lc)
    if have/len(T) >= .9 and a[0] is not None: P[c] = np.array(a, float); good.append(c)
n = len(T); W = 70; YRS = n/365.0
YEAR = [int(np.datetime64(int(t/1000), 's').astype('datetime64[Y]').astype(int)) + 1970 for t in T]
print(f"{len(good)} coins · {YRS:.1f}yr · {n} bars · warmup {W}\n")

# ---- signals -------------------------------------------------------------
def mom(c, i, lb):  return P[c][i]/P[c][i-lb]-1 if P[c][i-lb] > 0 else -9      # NEUTRAL: cross-sectional
def tsm(c, i, ma):  return P[c][i]/P[c][i-ma:i].mean()-1 if P[c][i-ma:i].mean() > 0 else -9  # TREND: vs own MA

FEE = 0.0004   # 4bps per unit turnover (maker + slippage) — same for both

def run(score, ev, K):
    """Dollar-neutral: long top-K by `score`, short bottom-K. Returns (eq, turn/yr, per-bar rets, bar years)."""
    eq = [1.0]; wp = {}; to_sum = 0.0; rets = []; yrs = []
    for i in range(W, n-1):
        if (i-W) % ev == 0:
            rank = sorted(good, key=lambda c: score(c, i), reverse=True)
            longs, shorts = rank[:K], rank[-K:]
            w = {c: 1.0/K for c in longs}
            for c in shorts: w[c] = w.get(c, 0) - 1.0/K
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good)
        ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)
        net = ret - to*FEE
        eq.append(eq[-1]*(1+net)); wp = w; to_sum += to; rets.append(net); yrs.append(YEAR[i])
    return np.array(eq), to_sum/YRS, np.array(rets), np.array(yrs)

def sh(r):   return r.mean()/r.std()*np.sqrt(365) if len(r) and r.std() > 0 else 0.0
def met(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2
    cagr = ((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))     # cagr, sharpe, maxDD, worst-half

# ---- parameter grids -----------------------------------------------------
NEUTRAL = [("neutral", lambda c,i,lb=lb: mom(c,i,lb), f"lb{lb}", ev, K)
           for lb in (14,30,60) for ev in (1,3) for K in (5,8)]
TREND   = [("trend",   lambda c,i,ma=ma: tsm(c,i,ma), f"ma{ma}", ev, K)
           for ma in (20,30,50) for ev in (1,3) for K in (5,8)]

def grid(cfgs, label):
    print(f"\n{'='*72}\n{label} — full parameter grid (same data, fees, construction)\n{'='*72}")
    print(f"{'param':<10}{'rebal':>6}{'K':>3}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>7}{'wHalf':>7}")
    print("-"*72)
    shs = []
    for _, score, pname, ev, K in cfgs:
        eq, turn, rets, yrs = run(score, ev, K)
        c, s, dd, wh = met(eq)
        shs.append(s)
        print(f"{pname:<10}{ev:>5}d{K:>3}{c:>+7.0f}%{s:>8.2f}{dd:>6.0f}%{wh:>7.2f}")
    shs = np.array(shs)
    print("-"*72)
    print(f"ROBUSTNESS across {len(shs)} configs:  mean Sharpe {shs.mean():.2f} · MIN {shs.min():.2f} · "
          f"max {shs.max():.2f} · {100*(shs>0.5).mean():.0f}% of configs Sharpe>0.5")
    return shs

sN = grid(NEUTRAL, "NEUTRAL  (cross-sectional momentum)")
sT = grid(TREND,   "TREND    (time-series vs own MA)")

# ---- head-to-head at each book's LIVE default, with per-year regime split + tail ----
print(f"\n{'='*72}\nHEAD-TO-HEAD at live defaults · per-YEAR Sharpe (regime) · tail\n{'='*72}")
defs = [("NEUTRAL lb30 rebal1 K5", lambda c,i: mom(c,i,30), 1, 5),
        ("TREND   ma30 rebal2 K8", lambda c,i: tsm(c,i,30), 2, 8)]
years = sorted(set(YEAR[W:n-1]))
hdr = f"{'book':<24}{'Sharpe':>7}{'CAGR':>7}{'maxDD':>7}{'wDay':>7}{'skew':>6}  " + " ".join(f"{y}" for y in years)
print(hdr); print("-"*len(hdr))
for name, score, ev, K in defs:
    eq, turn, rets, yrs = run(score, ev, K)
    c, s, dd, wh = met(eq)
    worst = rets.min()*100
    skew = float(((rets-rets.mean())**3).mean() / (rets.std()**3 + 1e-12))
    ycol = " ".join(f"{sh(rets[yrs==y]):>4.1f}" for y in years)
    print(f"{name:<24}{s:>7.2f}{c:>+6.0f}%{dd:>6.0f}%{worst:>+6.1f}%{skew:>6.2f}  {ycol}")

print(f"\n{'='*72}\nVERDICT\n{'='*72}")
print(f"  NEUTRAL grid:  mean Sharpe {sN.mean():.2f} · worst-config {sN.min():.2f}")
print(f"  TREND   grid:  mean Sharpe {sT.mean():.2f} · worst-config {sT.min():.2f}")
better = "NEUTRAL" if sN.mean() > sT.mean() else "TREND"
worse  = "TREND"   if better == "NEUTRAL" else "NEUTRAL"
print(f"  -> more robust across parameters: {better}.  Weaker / kill candidate: {worse}.")
print("  (Also weigh per-year consistency + tail skew above: a negative-skew book hides short-squeeze risk.)")
