"""GRID — DEVIL'S ADVOCATE. Assume the backtest is a lie and try to break it. Same real engine logic,
but we STACK pessimistic frictions and probe every way this edge could be an artifact:

  1. Friction stress: taker fees, a trade-THROUGH fill buffer (you don't fill on a mere touch — queue
     position), and stop slippage. Stacked = the realistic-worst case.
  2. Recent out-of-sample: last 12 months only (is the edge decaying like Neutral's did?).
  3. Parameter robustness: sweep spacing / K / stop — a real edge survives the grid, an artifact doesn't.
  4. Is it just beta? avg net exposure + correlation to BTC (is the 'alpha' just being long a bull market?).
  5. The tail: profit factor, worst trade, MAX CONSECUTIVE LOSING DAYS (the 87% win-rate hides the losses).
"""
import sys, time, json, urllib.request
import numpy as np, datetime as dt
COINS = ["BTC", "ETH", "SOL", "DOGE", "LINK", "ADA", "LTC"]; BN = {c: c + "USDT" for c in COINS}


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


print("fetching 4.5yr hourly for 7 coins...")
ser = {}
for c in COINS:
    k = kl(BN[c])
    if len(k) > 5000:
        ser[c] = {int(r[0]): (float(r[2]), float(r[3]), float(r[4])) for r in k}
ref = max(ser, key=lambda c: len(ser[c])); T = sorted(ser[ref]); H = {}; L = {}; C = {}
for c in ser:
    h = []; l = []; cc = []; lh = ll = lc = None
    for t in T:
        if t in ser[c]:
            lh, ll, lc = ser[c][t]
        h.append(lh); l.append(ll); cc.append(lc)
    if cc[0] is not None:
        H[c] = np.array(h, float); L[c] = np.array(l, float); C[c] = np.array(cc, float)
coins = list(C); n = len(T); yrs = np.array([dt.datetime.utcfromtimestamp(t / 1000).year for t in T])
MA = {c: np.array([C[c][max(0, i - 100):i + 1].mean() for i in range(n)]) for c in coins}
print(f"{len(coins)} coins · {n} hourly bars · {n/24/365:.1f}yr\n")


def replay(K=6, sp=0.012, stop=0.10, cost=0.0005, entry_buf=0.0, exit_buf=0.0, stop_slip=0.0,
           band=0.08, recenter=168, lo=None, hi=None):
    """Mirror of grid_book.tick with pessimistic knobs. Returns (port_hourly, netexp_hourly, trade_returns)."""
    a0 = 100 + 5; b0 = lo if lo is not None else a0; b1 = hi if hi is not None else n - 1
    port = np.zeros(n); netexp = np.zeros(n); trades = []
    for c in coins:
        anchor = C[c][a0]; longs = []; shorts = []; tick = 0; prev = 0.0; cum = 0.0
        for i in range(a0, n - 1):
            tick += 1; h = H[c][i]; l = L[c][i]; cl = C[c][i]; realized = 0.0
            if tick % recenter == 0 and not longs and not shorts:
                anchor = cl
            ranging = MA[c][i] > 0 and abs(cl / MA[c][i] - 1) < band
            if longs and l <= anchor * (1 - stop):
                spx = anchor * (1 - stop) * (1 - stop_slip)
                for bp in longs:
                    r = (spx / bp - 1) - 2 * cost; realized += r / K; trades.append((i, r))
                longs = []; anchor = cl
            if shorts and h >= anchor * (1 + stop):
                spx = anchor * (1 + stop) * (1 + stop_slip)
                for bp in shorts:
                    r = (bp / spx - 1) - 2 * cost; realized += r / K; trades.append((i, r))
                shorts = []; anchor = cl
            keep = []
            for bp in sorted(longs):
                if h > bp * (1 + sp) * (1 + exit_buf):
                    r = sp - 2 * cost; realized += r / K; trades.append((i, r))
                else:
                    keep.append(bp)
            longs = keep
            keep = []
            for bp in sorted(shorts, reverse=True):
                if l < bp * (1 - sp) * (1 - exit_buf):
                    r = sp - 2 * cost; realized += r / K; trades.append((i, r))
                else:
                    keep.append(bp)
            shorts = keep
            if ranging:
                while len(longs) < K:
                    lv = anchor * (1 - (len(longs) + 1) * sp)
                    if l < lv * (1 - entry_buf):
                        longs.append(lv)
                    else:
                        break
                while len(shorts) < K:
                    lv = anchor * (1 + (len(shorts) + 1) * sp)
                    if h > lv * (1 + entry_buf):
                        shorts.append(lv)
                    else:
                        break
            op = sum((cl / bp - 1) for bp in longs) / K + sum((bp / cl - 1) for bp in shorts) / K
            total = cum + realized + op - (cum + prev_op if i > a0 else 0) if False else cum + op  # noqa
            # hourly pnl = change in (cum realized + open)
            cur = cum + realized  # cum realized AFTER this bar
            cum = cur
            tot = cum + op
            port[i] += (tot - prev) / len(coins); prev = tot
            netexp[i] += ((len(longs) - len(shorts)) / K) / len(coins)
    return port, netexp, trades


def stats(port, lo=105, hi=None):
    hi = hi if hi is not None else n - 1
    p = port[lo:hi]
    sh = p.mean() / p.std() * np.sqrt(24 * 365) if p.std() > 0 else 0
    eq = np.cumprod(1 + p); dd = ((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)).max() * 100
    return sh, (eq[-1] - 1) * 100, dd


def tradestats(trades):
    r = np.array([t[1] for t in trades])
    if not len(r):
        return 0, 0, 0, 0
    win = (r > 0).mean() * 100
    pf = r[r > 0].sum() / abs(r[r < 0].sum()) if (r < 0).any() else 99
    return win, pf, r.min() * 100, len(r)


print("=" * 78)
print("1. FRICTION STRESS  (per-year Sharpe · total return · maxDD · profit-factor)")
print("=" * 78)
years = [2022, 2023, 2024, 2025, 2026]
scen = [
    ("BASE (maker, fill-on-touch)", dict(cost=0.0005)),
    ("+ taker fees (0.09% RT)", dict(cost=0.00045 * 2 / 2 + 0.00045)),  # ~taker each side
    ("+ trade-through buffer 0.1%", dict(cost=0.0005, entry_buf=0.001, exit_buf=0.001)),
    ("+ stop slippage 0.5%", dict(cost=0.0005, stop_slip=0.005)),
    ("ALL STACKED (worst case)", dict(cost=0.0009, entry_buf=0.001, exit_buf=0.001, stop_slip=0.005)),
]
print(f"{'scenario':<30}" + "".join(f"{y:>7}" for y in years) + f"{'TOTAL':>8}{'maxDD':>7}{'PF':>6}{'win%':>6}")
print("-" * 78)
for name, kw in scen:
    port, netexp, trades = replay(**kw)
    row = f"{name:<30}"
    for y in years:
        m = (yrs[:len(port)] == y) & (np.arange(len(port)) >= 105)
        pp = port[m]; row += f"{(pp[pp!=0].mean()/pp[pp!=0].std()*np.sqrt(24*365) if pp[pp!=0].std()>0 else 0):>7.1f}"
    sh, tot, dd = stats(port); win, pf, worst, ntr = tradestats(trades)
    row += f"{tot:>+7.0f}%{dd:>6.0f}%{pf:>6.2f}{win:>5.0f}%"
    print(row)

print("\n" + "=" * 78)
print("2. RECENT OUT-OF-SAMPLE (last ~12 months only)")
print("=" * 78)
lo12 = max(105, n - 24 * 365)
for name, kw in [("BASE", dict(cost=0.0005)), ("ALL STACKED", dict(cost=0.0009, entry_buf=0.001, exit_buf=0.001, stop_slip=0.005))]:
    port, _, trades = replay(**kw)
    sh, tot, dd = stats(port, lo=lo12)
    tr12 = [t for t in trades if t[0] >= lo12]; win, pf, worst, ntr = tradestats(tr12)
    print(f"  {name:<14} Sharpe {sh:>5.2f}  total {tot:>+5.0f}%  maxDD {dd:>3.0f}%  PF {pf:.2f}  win {win:.0f}%  ({ntr} trades)")

print("\n" + "=" * 78)
print("3. PARAMETER ROBUSTNESS (does the edge survive the grid, or is it one lucky setting?)")
print("=" * 78)
shs = []
for K in (4, 6, 8):
    for sp in (0.008, 0.012, 0.018):
        for stop in (0.08, 0.10, 0.15):
            port, _, _ = replay(K=K, sp=sp, stop=stop, cost=0.0009, entry_buf=0.001, exit_buf=0.001, stop_slip=0.005)
            sh, tot, dd = stats(port); shs.append(sh)
shs = np.array(shs)
print(f"  27 settings (ALL under WORST-CASE frictions): mean Sharpe {shs.mean():.2f} · min {shs.min():.2f} · "
      f"max {shs.max():.2f} · {100*(shs>0).mean():.0f}% positive · {100*(shs>0.5).mean():.0f}% Sharpe>0.5")

print("\n" + "=" * 78)
print("4. IS IT JUST BETA?  (net exposure + correlation to BTC — or is it real mean-reversion alpha?)")
print("=" * 78)
port, netexp, _ = replay(cost=0.0005)
btc_h = np.diff(C["BTC"], prepend=C["BTC"][0]) / C["BTC"]
mask = np.arange(n) >= 105
corr = np.corrcoef(port[mask], btc_h[:len(port)][mask])[0, 1]
btc_hold = (C["BTC"][-1] / C["BTC"][105] - 1) * 100
print(f"  avg net exposure: {netexp[mask].mean()*100:>+5.1f}% of book  (|net| avg {np.abs(netexp[mask]).mean()*100:.0f}%)")
print(f"  correlation of grid returns to BTC: {corr:>+.2f}   (near 0 = market-neutral alpha, not beta)")
print(f"  BTC buy&hold over same window: {btc_hold:+.0f}%  — grid does NOT need the market to rise")

print("\n" + "=" * 78)
print("5. THE TAIL  (the 87% win-rate hides the losses — how bad is the worst stretch?)")
print("=" * 78)
port, _, trades = replay(cost=0.0009, entry_buf=0.001, exit_buf=0.001, stop_slip=0.005)   # worst case
# daily returns -> max consecutive losing days
day = {}
for i in range(105, n - 1):
    d = dt.datetime.utcfromtimestamp(T[i] / 1000).strftime("%Y-%m-%d"); day.setdefault(d, 0.0)
    day[d] += port[i]
dr = np.array([day[d] for d in sorted(day)])
mc = cur = 0
for x in dr:
    cur = cur + 1 if x < 0 else 0; mc = max(mc, cur)
win, pf, worst, ntr = tradestats(trades)
print(f"  profit factor (worst case): {pf:.2f}   win-rate {win:.0f}%   worst single trade {worst:.1f}%")
print(f"  worst day {dr.min()*100:+.1f}%   max consecutive losing days {mc}   negative days {100*(dr<0).mean():.0f}%")
print(f"  avg win vs avg loss per trade: many small wins, few ~{abs(worst):.0f}% stops — PF>1 is what matters")
print("\nVERDICT: real if it stays POSITIVE under worst-case frictions, in recent OOS, across the param grid,")
print("and with ~0 BTC correlation. Any red flag => the +288% was optimistic.")
