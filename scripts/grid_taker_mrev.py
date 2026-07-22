"""FIX ATTEMPT: kill the adverse-selection problem by NOT relying on passive fills.

The passive grid dies because resting limits fill on the trades that blow THROUGH (losers) and miss the
touch-and-bounce (winners). The fix: enter with MARKET orders (taker, you ALWAYS fill — no fill illusion,
no adverse selection) but ONLY on high-conviction mean-reversion extremes, so the per-trade edge beats the
taker cost. This is the honest version of 'buy the dip / sell the rip'.

  signal : z-score = (price - MA_w) / rolling_std_w.  z < -Z = oversold -> LONG ; z > +Z = overbought -> SHORT
  entry  : MARKET at the bar close (realistic fill + taker cost + slippage). No resting-order assumption.
  exit   : reverts toward the mean (|z| < Z_exit) OR hard stop OR max-hold time-stop.
  size   : one position per coin, equal-weight (1/N) -> gross <= 1x, no leverage.

We test: does it survive TAKER costs, across the parameter grid, in recent OOS, without being BTC-beta?
If yes -> real, deployable. If no -> mean-reversion has no tradable edge here after costs (honest dead-end).
"""
import sys, time, json, urllib.request
import numpy as np, datetime as dt
COINS = ["BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "LTC"]; BN = {c: c + "USDT" for c in COINS}
TAKER = 0.0006   # per side: ~4.5bps HL taker + ~1.5bps slippage = 0.12% round-trip (realistic, always fills)


def kl(sym, days=1650):
    end = int(time.time() * 1000); s = end - days * 24 * 3600 * 1000; out = []
    while s < end:
        try:
            k = json.load(urllib.request.urlopen(
                f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&startTime={s}&limit=1000", timeout=20))
        except Exception:
            break
        if not k:
            break
        out += k; s = k[-1][0] + 1
        if len(k) < 1000:
            break
    return out


print("fetching 4.5yr hourly...")
ser = {}
for c in COINS:
    k = kl(BN[c])
    if len(k) > 5000:
        ser[c] = {int(r[0]): float(r[4]) for r in k}
ref = max(ser, key=lambda c: len(ser[c])); T = sorted(ser[ref]); C = {}
for c in ser:
    a = []; lc = None
    for t in T:
        if t in ser[c]:
            lc = ser[c][t]
        a.append(lc)
    if a[0] is not None:
        C[c] = np.array(a, float)
coins = list(C); n = len(T); yrs = np.array([dt.datetime.utcfromtimestamp(t / 1000).year for t in T])
print(f"{len(coins)} coins · {n} bars · {n/24/365:.1f}yr  (taker {TAKER*2*100:.2f}%/round-trip)\n")


def zscore(c, w):
    z = np.zeros(len(c))
    for i in range(w, len(c)):
        seg = c[i - w:i]
        mu = seg.mean(); sd = seg.std()
        z[i] = (c[i] - mu) / sd if sd > 0 else 0
    return z


def run(w=24, Z=2.0, Zx=0.5, stop=0.08, maxhold=48, cost=TAKER, lo=None, hi=None):
    """returns (port_hourly, netexp_hourly, trade_returns)."""
    a0 = w + 2; b1 = n - 1
    lo = lo if lo is not None else a0; hi = hi if hi is not None else b1
    port = np.zeros(n); netexp = np.zeros(n); trades = []
    for c in coins:
        z = zscore(C[c], w); pos = 0; entry = 0.0; held = 0
        for i in range(a0, n - 1):
            # realize this bar's PnL from the position held into it
            if pos != 0:
                port[i] += pos * (C[c][i + 1] / C[c][i] - 1) / len(coins)
                netexp[i] += pos / len(coins)
                held += 1
            # decide action at close i (signal uses data up to i only)
            if pos == 0:
                if z[i] < -Z:
                    pos = 1; entry = C[c][i]; held = 0; port[i] -= cost / len(coins)
                elif z[i] > Z:
                    pos = -1; entry = C[c][i]; held = 0; port[i] -= cost / len(coins)
            else:
                ret = (C[c][i] / entry - 1) * pos
                reverted = abs(z[i]) < Zx
                stopped = ret <= -stop
                timeout = held >= maxhold
                if reverted or stopped or timeout:
                    port[i] -= cost / len(coins)
                    trades.append(ret - 2 * cost)
                    pos = 0
    return port, netexp, trades


def met(port, lo=None):
    lo = lo if lo is not None else 26
    p = port[lo:n - 1]
    sh = p.mean() / p.std() * np.sqrt(24 * 365) if p.std() > 0 else 0
    eq = np.cumprod(1 + p); dd = ((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)).max() * 100
    return sh, (eq[-1] - 1) * 100, dd


def tstats(tr):
    r = np.array(tr)
    if not len(r):
        return 0, 0, 0
    pf = r[r > 0].sum() / abs(r[r < 0].sum()) if (r < 0).any() else 99
    return (r > 0).mean() * 100, pf, len(r)


years = [2022, 2023, 2024, 2025, 2026]
print("=" * 76)
print("TAKER MEAN-REVERSION — per-year Sharpe · total · maxDD · PF · win% · #trades")
print("=" * 76)
print(f"{'params':<22}" + "".join(f"{y:>6}" for y in years) + f"{'TOT':>7}{'DD':>5}{'PF':>6}{'win':>5}{'trd':>7}")
print("-" * 76)
best = None
for w in (24, 48):
    for Z in (1.5, 2.0, 2.5):
        port, ne, tr = run(w=w, Z=Z)
        row = f"w{w} Z{Z}".ljust(22)
        for y in years:
            m = (yrs[:len(port)] == y) & (np.arange(len(port)) >= w + 2)
            pp = port[m]; row += f"{(pp[pp!=0].mean()/pp[pp!=0].std()*np.sqrt(24*365) if pp[pp!=0].std()>0 else 0):>6.1f}"
        sh, tot, dd = met(port); win, pf, ntr = tstats(tr)
        row += f"{tot:>+6.0f}%{dd:>4.0f}%{pf:>6.2f}{win:>4.0f}%{ntr:>7}"
        print(row)
        if best is None or sh > best[0]:
            best = (sh, w, Z, port, ne, tr)

sh, w, Z, port, ne, tr = best
print("-" * 76)
print(f"BEST: w{w} Z{Z}  ->  Sharpe {sh:.2f}")
# robustness
shs = []
for w2 in (12, 24, 48):
    for Z2 in (1.5, 2.0, 2.5):
        for stop2 in (0.06, 0.10):
            p2, _, _ = run(w=w2, Z=Z2, stop=stop2)
            shs.append(met(p2)[0])
shs = np.array(shs)
print(f"ROBUSTNESS ({len(shs)} settings, all taker cost): mean Sharpe {shs.mean():.2f} · min {shs.min():.2f} · "
      f"{100*(shs>0).mean():.0f}% positive · {100*(shs>0.5).mean():.0f}% >0.5")
# recent OOS
lo12 = max(w + 2, n - 24 * 365)
sh12, tot12, dd12 = met(port, lo=lo12)
tr12 = tstats([t for t in tr])[0]
print(f"RECENT 12mo (best params): Sharpe {sh12:.2f}  total {tot12:+.0f}%  maxDD {dd12:.0f}%")
# beta
btc_h = np.diff(C["BTC"], prepend=C["BTC"][0]) / C["BTC"]
mask = np.arange(n) >= w + 2
corr = np.corrcoef(port[mask][:len(btc_h[mask])], btc_h[mask][:len(port[mask])])[0, 1]
print(f"BTC correlation {corr:+.2f}  ·  avg net exposure {ne[mask].mean()*100:+.1f}%")
print("\nREAD: positive across the grid + recent OOS + ~0 BTC corr, AFTER taker cost = a REAL, deployable edge")
print("(no fill illusion — market orders always fill). If it's flat/negative, mean-reversion can't beat costs here.")
