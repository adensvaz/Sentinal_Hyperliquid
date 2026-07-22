"""Backtest the 'Treasurer' profit-sweep / payout overlay.

The overlay is a DETERMINISTIC transform of an equity curve — it cannot create return.
So we measure the TRADE-OFF frontier: growth given up vs drawdown-reduction + income delivered,
across sweep rates, and test a high-water-mark (HWM) partial sweep vs a naive periodic sweep.

Return stream = a realistic 4-book portfolio built on REAL Binance daily prices (real crashes):
  - Trend  : dollar-neutral cross-sectional momentum (long top / short bottom by 30d return)
  - Champion: long-only top-K momentum, gated by BTC>100d-MA regime (else cash)
  - Carry  : market-neutral funding sleeve, modelled to its documented profile (Sharpe~1.4, low vol)
Portfolio = equal-weight, daily. Then block-bootstrap (preserves momentum + crash clustering)
and apply the Treasurer grid to thousands of paths.
"""
import urllib.request, json, math, numpy as np

rng = np.random.default_rng(7)
COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT",
         "AVAXUSDT","DOGEUSDT","LINKUSDT","DOTUSDT","LTCUSDT","ATOMUSDT"]

def fetch_daily(sym, limit=1500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1d&limit={limit}"
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                k = json.load(r)
            return {int(row[0]): float(row[4]) for row in k}   # closeTime->close
        except Exception:
            continue
    return {}

print("fetching real Binance daily closes ...")
data = {c: fetch_daily(c) for c in COINS}
data = {c: v for c, v in data.items() if len(v) > 400}
# align on common timestamps
common = sorted(set.intersection(*[set(v.keys()) for v in data.values()]))
syms = list(data.keys())
px = np.array([[data[s][t] for s in syms] for t in common])   # [days, coins]
rets = px[1:] / px[:-1] - 1.0                                  # daily returns [days-1, coins]
ndays, ncoins = rets.shape
print(f"  {len(syms)} coins, {ndays} daily bars (~{ndays/365:.1f}yr)")

btc = px[:, 0]
ma100 = np.array([btc[max(0,i-100):i+1].mean() for i in range(len(btc))])
mom30 = np.full((len(px), ncoins), np.nan)
for i in range(30, len(px)):
    mom30[i] = px[i]/px[i-30] - 1.0

def cs_momentum_daily(k=4, neutral=True):
    """Daily book return: long top-k / short bottom-k by 30d momentum."""
    out = np.zeros(ndays)
    for i in range(1, ndays):
        m = mom30[i-1]                       # yesterday's signal -> today's return (no lookahead)
        if np.isnan(m).any(): continue
        order = np.argsort(m)
        longs = order[-k:]; shorts = order[:k]
        r = rets[i]
        lo = r[longs].mean()
        if neutral:
            out[i] = 0.5*lo - 0.5*r[shorts].mean()   # dollar-neutral
        else:
            in_regime = btc[i-1] > ma100[i-1]         # champion regime gate
            out[i] = lo if in_regime else 0.0
    return out

trend = cs_momentum_daily(k=4, neutral=True)          # real
champ = cs_momentum_daily(k=4, neutral=False)         # real, regime-gated
# Carry: market-neutral funding sleeve, modelled to documented profile (Sharpe~1.4, ~8%/yr vol)
carry = rng.normal(0.11/365, 0.08/math.sqrt(365), ndays)
# scale the directional books to a sane per-book vol target (~1.2% daily) so blend is realistic
def voltar(x, tgt=0.012):
    s = x.std()
    return x*(tgt/s) if s>0 else x
trend, champ = voltar(trend), voltar(champ)
port = (trend + champ + carry) / 3.0                  # equal-weight portfolio, daily

def stats(daily):
    eq = np.cumprod(1+daily); peak = np.maximum.accumulate(eq)
    dd = (peak-eq)/peak
    ann = (eq[-1]**(365/len(daily)) - 1)
    sharpe = daily.mean()/daily.std()*math.sqrt(365)
    return ann, sharpe, dd.max()
a,s,d = stats(port)
print(f"  base portfolio: {a*100:+.0f}%/yr  Sharpe {s:.2f}  maxDD {d*100:.0f}%")
at,_,dt = stats(trend); print(f"  (standalone Trend book: {at*100:+.0f}%/yr, maxDD {dt*100:.0f}%  <- deep, the crash case)")

# ---------------- Treasurer overlay ----------------
def treasurer(daily, sweep_frac, payout_days=7, principal=40000.0, hwm=True):
    trading = principal; W = principal; vault = 0.0; paid = 0.0
    tcurve = np.empty(len(daily)); total = np.empty(len(daily))
    for i, r in enumerate(daily):
        trading *= (1+r)
        if hwm:
            if trading > W:                       # sweep a slice of NEW profit only
                s = sweep_frac*(trading - W); trading -= s; vault += s; W = trading
        else:                                     # naive: sweep frac of everything above principal each day
            if trading > principal:
                s = sweep_frac*(trading-principal)/60.0; trading -= s; vault += s
        if payout_days and (i+1) % payout_days == 0:
            paid += vault; vault = 0.0            # cash taken off the table
        tcurve[i] = trading; total[i] = trading+vault+paid
    return tcurve, total, paid, vault

def maxdd(curve):
    peak = np.maximum.accumulate(curve); return ((peak-curve)/peak).max()

# ---------------- block-bootstrap many futures ----------------
def bootstrap_paths(daily, n=2000, length=1250, block=20):
    idx = np.arange(len(daily))
    paths = np.empty((n, length))
    for p in range(n):
        out = [];
        while len(out) < length:
            st = rng.integers(0, len(daily)-block)
            out.extend(daily[st:st+block])
        paths[p] = out[:length]
    return paths

PRIN = 40000.0
def run_grid(daily, label):
    a,s,d = stats(daily)
    print(f"\n=== {label}: {a*100:+.0f}%/yr, Sharpe {s:.2f}, maxDD {d*100:.0f}% ===")
    paths = bootstrap_paths(daily, n=2000, length=1250, block=20)
    print(f"{'sweep':>6} | {'final wealth':>13} | {'growth 5yr':>11} | {'TOTAL maxDD':>11} | {'income/yr':>10} | {'income yield':>11} | {'worst-5%':>10}")
    print("-"*94)
    base=None
    for sf in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        finals=[]; totdd=[]; incomes=[]
        for pth in paths:
            tc, tot, paid, vault = treasurer(pth, sf, payout_days=7, principal=PRIN, hwm=True)
            finals.append(tot[-1]); totdd.append(maxdd(tot)); incomes.append(paid/(1250/365))
        finals=np.array(finals); med=np.median(finals); grow=med/PRIN-1
        if sf==0: base=med
        inc=np.median(incomes)
        print(f"{sf*100:>5.0f}% | {med:>13,.0f} | {grow*100:>+10.0f}% | {np.median(totdd)*100:>10.0f}% | {inc:>10,.0f} | {inc/PRIN*100:>10.1f}% | {np.percentile(finals,5):>10,.0f}")
    print(f"  cost of 100% vs 0% sweep over 5yr: {(base-med)/PRIN*100:.1f}% of capital")

print("\nblock-bootstrapping 2000 x 5yr futures + sweeping the grid ...")
run_grid(port, "COMBINED PORTFOLIO (as backtested)")
port_hi = port + (0.20 - stats(port)[0])/365.0        # shift drift to the ~20%/yr documented level
run_grid(port_hi, "SAME PORTFOLIO scaled to ~20%/yr (documented range)")

# HWM vs naive at 40%
print("\nHWM partial-sweep vs NAIVE periodic sweep (both 40%, on the deep Trend book):")
tp = bootstrap_paths(trend, n=1500, length=1250, block=20)
for label,hwm in [("HWM (only new highs)",True),("NAIVE (sweep above principal)",False)]:
    f=[]; dd=[]
    for pth in tp:
        tc,tot,paid,vault=treasurer(pth,0.4,payout_days=7,principal=PRIN,hwm=hwm)
        f.append(tot[-1]); dd.append(maxdd(tot))
    print(f"  {label:32}: median wealth {np.median(f):>10,.0f}  total maxDD {np.median(dd)*100:>4.0f}%")
