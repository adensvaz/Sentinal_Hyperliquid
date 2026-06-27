"""LONG multi-year backtest of the residual-momentum strategy on Binance public daily data (no auth).
Validates the alpha across multiple market regimes (2021 bull, 2022 bear, 2023-26). Run:
  ./.venv/bin/python scripts/bt_longbinance.py"""
import sys, time as _t, httpx
sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import simulate
from sentinel.util.concurrency import pmap

COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM",
         "NEAR","XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ"]
H = "https://data-api.binance.vision"

def klines(sym, end=None):
    p = {"symbol": sym+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    return httpx.get(H+"/api/v3/klines", params=p, timeout=20).json()

def fetch(coin):
    rows = []
    p1 = klines(coin)
    if not isinstance(p1, list) or not p1: return None
    oldest = int(p1[0][0])
    p2 = klines(coin, end=oldest-1)
    allrows = (p2 if isinstance(p2, list) else []) + p1
    return coin, {int(r[0]): (float(r[4]), float(r[5])) for r in allrows}

print("fetching ~5yr daily history from Binance (no auth)...")
series = {c: d for c, d in (r for r in pmap(fetch, COINS, workers=10) if r)}
print(f"got {len(series)} coins")

# align to the most-complete coin's timeline, forward-fill, drop <90% coverage
ref = max(series, key=lambda c: len(series[c]))
common = sorted(series[ref])
closes, vols, good = {}, {}, []
for c in series:
    ac, av, have, lc, lv = [], [], 0, None, None
    for t in common:
        if t in series[c]: lc, lv = series[c][t]; have += 1
        ac.append(lc); av.append(lv)
    if have/len(common) >= 0.90 and ac[0] is not None:
        closes["E-"+c+"-USDT"] = ac; vols["E-"+c+"-USDT"] = av; good.append("E-"+c+"-USDT")
T = common
print(f"aligned: {len(good)} coins, {len(T)} daily bars "
      f"({_t.strftime('%Y-%m-%d',_t.localtime(T[0]/1000))} -> {_t.strftime('%Y-%m-%d',_t.localtime(T[-1]/1000))})\n")

hist = {"universe": good, "T": T, "closes": closes, "vols": vols, "interval_h": 24.0}
cfg = load_config(); cfg.signal.momentum_horizons = ["1day","3day","7day","14day"]
fee = 0.00025 if cfg.execution.maker else 0.00075
slip = 2.0 if cfg.execution.maker else cfg.execution.slippage_bps

def yr(curve):
    b = {}
    for ts, eq, _g in curve:
        y = _t.strftime("%Y", _t.localtime(ts/1000)); b.setdefault(y, [eq, eq])[1] = eq
    return {y: (v[1]/v[0]-1)*100 for y, v in sorted(b.items())}

m = simulate(hist, cfg, rebalance_every=1, taker_fee=fee, slippage_bps=slip)
if "error" in m: print("ERROR:", m["error"]); sys.exit()
print("="*58)
print(f"  RESIDUAL-MOMENTUM, {len(T)} daily bars (~{len(T)/365:.1f} years)")
print("="*58)
print(f"  net return     {m['net_return_pct']:+.0f}%")
print(f"  annualized     {m['ann_return_pct']:+.0f}% / yr")
print(f"  Sharpe         {m['sharpe']:.2f}")
print(f"  max drawdown   {m['max_dd_pct']:.1f}%")
print(f"  hit rate       {m['hit_rate_pct']:.0f}% of days")
print(f"  mean |book beta| {m['book_beta_pre']:.3f}")
print("\n  per-year return:")
for y, r in yr(m["curve"]).items(): print(f"    {y}:  {r:+6.1f}%")
