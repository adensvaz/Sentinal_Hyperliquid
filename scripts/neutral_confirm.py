"""DEFINITIVE confirmation of the hypothesis: does rebalancing the dollar-neutral cross-sectional
MOMENTUM book WEEKLY (vs DAILY) give a real, durable edge?

Done well:
  - Deep history: paginate Binance daily klines back to each coin's listing (~2018 for BTC/ETH).
  - DYNAMIC universe: a coin is tradable only once it has enough history — coins ENTER as they list.
    This both lengthens the sample AND cuts survivorship bias (we don't assume today's 26 always existed).
  - Head-to-head DAILY vs WEEKLY across lookbacks; per-year; fee sensitivity; block-bootstrap significance.
  - Honest drawdown + universe-growth transparency.
"""
import sys, httpx, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(3)
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ",
         "TRX","FTM","MANA","AXS","EGLD","CHZ","ENJ","ZEC","DASH","WAVES","KSM","COMP"]

def kl(sym, end=None):
    p = {"symbol": sym, "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    try:
        return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=25).json()
    except Exception:
        return []

def deep(c, max_calls=5):
    """Paginate back to listing: newest 1000, then older, until exhausted."""
    out, end = {}, None
    for _ in range(max_calls):
        rows = kl(c + "USDT", end=end)
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            out[int(r[0])] = float(r[4])
        end = int(rows[0][0]) - 1
        if len(rows) < 1000:
            break
    return (c, out) if len(out) > 200 else None

raw = {c: d for c, d in (r for r in pmap(deep, COINS, workers=10) if r)}
master = sorted(set().union(*[set(d) for d in raw.values()]))   # union daily grid
# keep only days from when >=8 coins exist (avoids a 2-coin early book)
P = {c: np.array([raw[c].get(t, np.nan) for t in master], float) for c in raw}
avail = {c: ~np.isnan(P[c]) for c in P}
ncoins = np.array([sum(avail[c][i] for c in P) for i in range(len(master))])
start = int(np.argmax(ncoins >= 8))
master = master[start:]; P = {c: P[c][start:] for c in P}; avail = {c: avail[c][start:] for c in P}
n = len(master); coins = list(P); YRS = n/365.0
YEAR = np.array([datetime.fromtimestamp(t/1000, tz=timezone.utc).year for t in master])
d0 = datetime.fromtimestamp(master[0]/1000, tz=timezone.utc).date()
d1 = datetime.fromtimestamp(master[-1]/1000, tz=timezone.utc).date()

def usable(c, i, lb):
    return (avail[c][i] and avail[c][i+1] and i-lb >= 0 and avail[c][i-lb]
            and P[c][i] > 0 and P[c][i-lb] > 0)
def mom(c, i, lb): return P[c][i]/P[c][i-lb]-1

def run(lb, ev, K, fee, universe=None):
    U = universe or coins
    eq = [1.0]; wp = {}; rets = []
    for i in range(0, n-1):
        if i % ev == 0 or not wp:
            elig = [c for c in U if usable(c, i, lb)]
            if len(elig) >= 2*K+2:
                rank = sorted(elig, key=lambda c: mom(c, i, lb), reverse=True)
                w = {c: 1.0/K for c in rank[:K]}
                for c in rank[-K:]: w[c] = w.get(c, 0) - 1.0/K
            else:
                w = {}
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in U)
        ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in U if w.get(c) and usable(c, i, lb))
        eq.append(eq[-1]*(1+ret-to*fee)); wp = w; rets.append(ret-to*fee)
    return np.array(eq), np.array(rets)

def met(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = ((eq[-1]/eq[0])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))

print(f"DEEP history · {len(coins)} coins · {d0} -> {d1} ({YRS:.1f}yr, {n} bars) · dynamic universe")
print(f"universe size: {int(ncoins[start])} coins at start -> {sum(avail[c][-1] for c in coins)} now\n")

print("[1] HEAD-TO-HEAD across lookbacks (K=5/side, 8bps) — DAILY vs WEEKLY")
print(f"    {'lookback':<10}{'DAILY Sharpe':>14}{'WEEKLY Sharpe':>15}{'  weekly CAGR/maxDD/wHalf':>26}")
for lb in (7, 14, 20, 30, 45):
    _, rd = run(lb, 1, 5, 0.0008); ed = np.cumprod(1+np.r_[0, rd])
    ew, rw = run(lb, 7, 5, 0.0008)
    cd, sd, _, _ = met(ed); cw, sw, dw, whw = met(ew)
    print(f"    lb{lb:<8}{sd:>14.2f}{sw:>15.2f}{('  '+f'{cw:+.0f}% / {dw:.0f}% / {whw:.2f}'):>26}")

print("\n[2] PER-YEAR (lb14, K=5, 8bps) — does WEEKLY beat DAILY year by year?")
ew, rw = run(14, 7, 5, 0.0008); _, rd = run(14, 1, 5, 0.0008)
yrs = sorted(set(YEAR[1:]))
wins = 0; tot = 0
for y in yrs:
    m = YEAR[1:] == y
    if m.sum() < 30: tag = " (part)"
    else: tag = ""
    sw = rw[m].mean()/rw[m].std()*np.sqrt(365) if rw[m].std() > 0 else 0
    sd = rd[m].mean()/rd[m].std()*np.sqrt(365) if rd[m].std() > 0 else 0
    win = sw > sd; wins += win; tot += 1
    print(f"    {y}{tag:<7} DAILY {sd:>5.2f}   WEEKLY {sw:>5.2f}   -> {'WEEKLY' if win else 'daily'}")
print(f"    weekly wins {wins}/{tot} years")

print("\n[3] FEE SENSITIVITY (lb14 weekly, K=5)")
for bps in (4, 8, 15, 25, 40):
    ew, _ = run(14, 7, 5, bps/1e4); c, s, dd, wh = met(ew)
    print(f"    {bps:>2}bps: CAGR {c:>+4.0f}%  Sharpe {s:.2f}  maxDD {dd:.0f}%  wHalf {wh:.2f}")

print("\n[4] BLOCK-BOOTSTRAP SIGNIFICANCE (21d blocks, 2000x) — Sharpe(weekly) - Sharpe(daily)")
ew, rw = run(14, 7, 5, 0.0008); _, rd = run(14, 1, 5, 0.0008)
L = min(len(rw), len(rd)); rw, rd = rw[:L], rd[:L]; bs = 21; nb = L//bs; diffs = []
def shp(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
for _ in range(2000):
    idx = np.concatenate([np.arange(s, s+bs) for s in rng.integers(0, L-bs, size=nb)])
    diffs.append(shp(rw[idx]) - shp(rd[idx]))
diffs = np.array(diffs)
print(f"    mean {diffs.mean():+.2f} · P(weekly>daily) = {100*(diffs>0).mean():.0f}% · [p5 {np.percentile(diffs,5):+.2f}, p95 {np.percentile(diffs,95):+.2f}]")

print("\n[5] FULL-PERIOD SUMMARY (lb14, K=5, 8bps)")
for ev, lab in ((1, "DAILY  "), (7, "WEEKLY ")):
    if ev == 1: eq = np.cumprod(1+np.r_[0, run(14, 1, 5, 0.0008)[1]])
    else: eq, _ = run(14, ev, 5, 0.0008)
    c, s, dd, wh = met(eq)
    print(f"    {lab} CAGR {c:>+4.0f}%  Sharpe {s:.2f}  maxDD {dd:.0f}%  worst-half {wh:.2f}")
print("-"*72)
print("VERDICT: WEEKLY confirmed over DAILY if [2] wins most years AND [4] P(weekly>daily) is high.")
print("Caveat still: dollar-neutral != low-drawdown — note the maxDD. Delisted coins still absent.")
