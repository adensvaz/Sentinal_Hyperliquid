"""Regime-conditional sleeve toggle: is there an edge in dropping one side
of a dollar-neutral book when the market regime is extreme?

Thesis: in a strong bull, the short sleeve bleeds (everything rises) → drop it, ride longs only.
        in a strong bear, the long sleeve bleeds → drop it, ride shorts only.

Test on real 4yr Binance daily data across Trend + Carry-proxy books.
Compare: always-both-sides vs regime-toggled (BTC regime gate).
"""
import urllib.request, json, math, numpy as np

rng = np.random.default_rng(42)
COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT",
         "AVAXUSDT","DOGEUSDT","LINKUSDT","DOTUSDT","LTCUSDT","ATOMUSDT",
         "MATICUSDT","NEARUSDT","FILUSDT","AAVEUSDT"]

def fetch_daily(sym, limit=1500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1d&limit={limit}"
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r: return {int(row[0]): float(row[4]) for row in json.load(r)}
        except: continue
    return {}

print("fetching Binance daily closes ...")
data = {c: fetch_daily(c) for c in COINS}
data = {c: v for c, v in data.items() if len(v) > 400}
common = sorted(set.intersection(*[set(v.keys()) for v in data.values()]))
syms = list(data.keys())
px = np.array([[data[s][t] for s in syms] for t in common])
rets = px[1:] / px[:-1] - 1.0
ndays, ncoins = rets.shape
print(f"  {len(syms)} coins, {ndays} days (~{ndays/365:.1f}yr)")

# BTC regime
btc = px[:, 0]
ma50  = np.array([btc[max(0,i-50):i+1].mean() for i in range(len(btc))])
ma100 = np.array([btc[max(0,i-100):i+1].mean() for i in range(len(btc))])
ma200 = np.array([btc[max(0,i-200):i+1].mean() for i in range(len(btc))])

# 30d momentum signal
mom30 = np.full((len(px), ncoins), np.nan)
for i in range(30, len(px)):
    mom30[i] = px[i]/px[i-30] - 1.0

def stats(daily_rets):
    eq = np.cumprod(1+daily_rets)
    ann = eq[-1]**(365/len(daily_rets)) - 1
    sharpe = daily_rets.mean()/daily_rets.std()*math.sqrt(365) if daily_rets.std()>0 else 0
    peak = np.maximum.accumulate(eq); dd = ((peak-eq)/peak).max()
    return ann, sharpe, dd

K = 4  # top/bottom K names

def run_book(mode="both", regime_ma="ma100"):
    """mode: 'both' | 'regime_toggle' | 'longs_only' | 'shorts_only'"""
    daily = np.zeros(ndays)
    ma = {"ma50": ma50, "ma100": ma100, "ma200": ma200}[regime_ma]

    for i in range(1, ndays):
        m = mom30[i-1]
        if np.isnan(m).any(): continue
        order = np.argsort(m)
        longs_idx = order[-K:]   # strongest uptrends
        shorts_idx = order[:K]   # strongest downtrends
        r = rets[i]

        bull = btc[i-1] > ma[i-1]   # yesterday's regime -> today's return

        long_ret = r[longs_idx].mean()
        short_ret = -r[shorts_idx].mean()  # short = negative of return

        if mode == "both":
            daily[i] = 0.5 * long_ret + 0.5 * short_ret
        elif mode == "longs_only":
            daily[i] = long_ret
        elif mode == "shorts_only":
            daily[i] = short_ret
        elif mode == "regime_toggle":
            if bull:
                # bull regime: drop shorts, ride longs only (full size)
                daily[i] = long_ret
            else:
                # bear regime: drop longs, ride shorts only (full size)
                daily[i] = short_ret
        elif mode == "regime_tilt":
            # softer: in bull, tilt 75% long / 25% short; in bear, opposite
            if bull:
                daily[i] = 0.75 * long_ret + 0.25 * short_ret
            else:
                daily[i] = 0.25 * long_ret + 0.75 * short_ret
        elif mode == "regime_toggle_half":
            # toggle but keep half-size on the dropped side (hedge)
            if bull:
                daily[i] = 0.75 * long_ret + 0.25 * short_ret
            else:
                daily[i] = 0.25 * long_ret + 0.75 * short_ret
    return daily

# ============ TEST 1: Trend book (cross-sectional momentum L/S) ============
print("\n" + "="*80)
print("TEST 1: TREND BOOK (cross-sectional momentum, top-4 / bottom-4)")
print("="*80)

for regime_ma in ["ma50", "ma100", "ma200"]:
    print(f"\n--- Regime gate: BTC vs {regime_ma.upper()} ---")
    print(f"  {'MODE':25} {'CAGR':>8} {'Sharpe':>8} {'maxDD':>8} | verdict")
    print("  " + "-"*72)

    base = run_book("both", regime_ma)
    ba, bs, bd = stats(base)

    for mode in ["both", "regime_toggle", "regime_tilt", "longs_only", "shorts_only"]:
        d = run_book(mode, regime_ma)
        a, s, dd = stats(d)

        if mode == "both":
            verdict = "(baseline — always dollar-neutral)"
        elif s > bs * 1.05 and dd < bd:
            verdict = "BETTER Sharpe + less DD"
        elif s > bs * 1.05:
            verdict = "better Sharpe, more DD"
        elif s < bs * 0.95 and dd > bd:
            verdict = "WORSE on both"
        elif s < bs * 0.95:
            verdict = "worse Sharpe"
        else:
            verdict = "~same"

        print(f"  {mode:25} {a*100:>+7.1f}% {s:>7.2f} {dd*100:>7.1f}% | {verdict}")

# ============ TEST 2: by-year breakdown ============
print("\n" + "="*80)
print("TEST 2: YEAR-BY-YEAR (Trend book, BTC vs MA100)")
print("="*80)
both = run_book("both", "ma100")
toggle = run_book("regime_toggle", "ma100")
tilt = run_book("regime_tilt", "ma100")

# split by calendar year
days_per_bar = (common[-1] - common[0]) / (len(common)-1)
import datetime
dates = [datetime.datetime.utcfromtimestamp(t/1000) for t in common[1:]]  # align with rets

years = sorted(set(d.year for d in dates))
print(f"\n  {'YEAR':>6} | {'BOTH Sharpe':>12} {'TOGGLE Sharpe':>14} {'TILT Sharpe':>12} | {'BOTH ret':>9} {'TOGGLE ret':>11} {'TILT ret':>9}")
print("  " + "-"*90)
for yr in years:
    mask = np.array([d.year == yr for d in dates])
    if mask.sum() < 30: continue
    ba, bs, _ = stats(both[mask])
    ta, ts, _ = stats(toggle[mask])
    la, ls, _ = stats(tilt[mask])
    win = "TOGGLE" if ts > bs*1.05 else ("TILT" if ls > bs*1.05 else "BOTH")
    print(f"  {yr:>6} | {bs:>11.2f} {ts:>13.2f} {ls:>11.2f} | {ba*100:>+8.1f}% {ta*100:>+10.1f}% {la*100:>+8.1f}%  <- {win}")

# ============ TEST 3: regime-aware sizing (reduce short size in bull, not eliminate) ============
print("\n" + "="*80)
print("TEST 3: GRADUATED REGIME TILT (how much to shift, BTC vs MA100)")
print("="*80)
print(f"\n  {'long_wt':>8} {'short_wt':>9} | {'CAGR':>8} {'Sharpe':>8} {'maxDD':>8}")
print("  " + "-"*52)
for lw_bull in [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]:
    sw_bull = 1.0 - lw_bull
    daily = np.zeros(ndays)
    for i in range(1, ndays):
        m = mom30[i-1]
        if np.isnan(m).any(): continue
        order = np.argsort(m)
        r = rets[i]
        long_ret = r[order[-K:]].mean()
        short_ret = -r[order[:K]].mean()
        bull = btc[i-1] > ma100[i-1]
        if bull:
            daily[i] = lw_bull * long_ret + sw_bull * short_ret
        else:
            daily[i] = sw_bull * long_ret + lw_bull * short_ret
    a, s, dd = stats(daily)
    label = f"bull={lw_bull:.0%}L/{sw_bull:.0%}S"
    best = " <-- BEST" if abs(lw_bull-0.50)<0.01 or s > 1.0 else ""
    print(f"  {label:>17} | {a*100:>+7.1f}% {s:>7.2f} {dd*100:>7.1f}%{best}")

# ============ TEST 4: does the short side actually LOSE in bulls? ============
print("\n" + "="*80)
print("TEST 4: DECOMPOSE — does the short sleeve actually bleed in bull regimes?")
print("="*80)
bull_mask = np.array([btc[i-1] > ma100[i-1] if i>0 else False for i in range(1, ndays+1)])
bear_mask = ~bull_mask
longs_d = run_book("longs_only", "ma100")
shorts_d = run_book("shorts_only", "ma100")
both_d = run_book("both", "ma100")

print(f"\n  {'':>20} {'BULL days':>12} {'BEAR days':>12} {'ALL':>12}")
print("  " + "-"*58)
for label, d in [("Long sleeve", longs_d), ("Short sleeve", shorts_d), ("Both (neutral)", both_d)]:
    bull_r = d[bull_mask[:len(d)]].mean()*365*100
    bear_r = d[bear_mask[:len(d)]].mean()*365*100
    all_r = d.mean()*365*100
    print(f"  {label:>20} {bull_r:>+11.1f}% {bear_r:>+11.1f}% {all_r:>+11.1f}%")

print(f"\n  Bull days: {bull_mask.sum()}  Bear days: {bear_mask.sum()}")
