"""Close the loop: backtest the ACTUAL production MomentumRegimeStrategy.build_book() over 5.5yr,
proving the deployed code (not the research scripts' inline math) reproduces the improvement.
Compares the OLD config (equal-weight, no breadth) vs the NEW live config (breadth + capped mom-weight),
each run through the same live vol-target/DD-throttle overlay."""
import sys, httpx, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
from sentinel.config import Config, ChampionCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.momentum_regime import MomentumRegimeStrategy

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
n = len(T); FEE = 0.0004; W = 110; YRS = n/365.0; EQ = 1_000_000.0
SYM = {c: f"E-{c}-USDT" for c in good}
registry = ContractRegistry([{"symbol": SYM[c], "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
    "multiplierCoin": c, "pricePrecision": 6, "minOrderVolume": 1, "maxMarketVolume": 1e12,
    "minLever": 0, "maxLever": 100, "status": 1} for c in good])

def cfg(**ch):
    base = dict(top_k=5, lookback=30, regime_ma=100, dual_confirm=False, target_gross=1.0)
    base.update(ch); return Config(strategy="champion", champion=ChampionCfg(**base))
OLD = MomentumRegimeStrategy(cfg())  # equal-weight, no breadth
NEW = MomentumRegimeStrategy(cfg(breadth_scale=True, breadth_ma=100, breadth_lo=0.30, breadth_hi=0.80,
                                 mom_weighted=True, max_name_weight=0.40))

def signal_returns(strat):
    """Run the production build_book each day -> daily book return + turnover (signal gross, pre-overlay)."""
    rets, tos = [], []; wprev = {}
    for i in range(W, n-1):
        closes = {SYM[c]: list(P[c][:i+1]) for c in good}
        prices = {SYM[c]: float(P[c][i]) for c in good}
        book = strat.build_book(closes, prices, registry, equity=EQ, gross_scale=1.0)
        w = {sym: pos.target_notional / EQ for sym, pos in book.positions.items()}  # fraction of equity
        tos.append(sum(abs(w.get(SYM[c], 0) - wprev.get(SYM[c], 0)) for c in good))
        rets.append(sum(w.get(SYM[c], 0) * (P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0))
        wprev = w
    return np.array(rets), np.array(tos)

def overlay(rets, tos):
    eq = 10_000.0; peak = eq; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak
        gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1 + g*rets[t] - g*tos[t]*FEE); peak = max(peak, eq); out.append(eq); rv.append(rets[t])
    return np.array(out)

def stats(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = (eq[-1]/eq[0])**(1/YRS)-1
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    return eq[-1], cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))

print(f"PRODUCTION-CODE backtest ($10k, through live overlay, {YRS:.1f}yr, {len(good)} coins)\n")
print(f"{'config (via build_book)':<34}{'final $':>11}{'CAGR':>6}{'Sharpe':>8}{'maxDD':>7}{'wHalf':>7}")
print("-"*73)
for lab, strat in [("OLD  equal-wt, no breadth", OLD), ("NEW  breadth + capped mom-wt", NEW)]:
    fin, c, s, d, wh = stats(overlay(*signal_returns(strat)))
    print(f"{lab:<34}{fin:>10,.0f}{c*100:>5.0f}%{s:>8.2f}{d*100:>6.0f}%{wh:>7.2f}")
print("-"*73)
print("Expectation (from research): NEW lifts Sharpe ~1.25->1.29, cuts maxDD ~35%->24%, wHalf ~1.10->1.20.")
