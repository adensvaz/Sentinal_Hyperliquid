"""Champion improvement research: layer candidate rules/maths onto the exact 5.5yr harness and measure.
Baseline = CURRENT LIVE champion: rank 30d momentum, top5, equal-weight, long-only,
           regime = BTC>100dMA (else cash), daily rebalance, 4bps/turn.
We test, one change at a time then best combos:
  RANKING : risk-adjusted momentum (mom/vol), multi-horizon blend (30+90), skip-the-top
  SIZING  : inverse-vol (risk parity), momentum-weighted
  REGIME  : market breadth (% universe >100dMA), MA-slope filter, ETH co-confirm
  CADENCE : weekly / monthly rebalance (churn vs freshness)
  GATES   : correlation/concentration throttle, trailing-high (don't buy extended)
Metrics: CAGR, Sharpe, maxDD, worst-half Sharpe (out-of-sample robustness), turnover/yr.
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
n = len(T); FEE = 0.0004; W = 110; YRS = n/365.0; K = 5

# ---------- primitives ----------
def mom(c, i, lb=30):
    if P[c][i-lb] <= 0: return -1e9
    return P[c][i]/P[c][i-lb]-1
def vol(c, i, k=20):
    a = P[c][i-k:i+1]; r = a[1:]/a[:-1]-1
    s = r.std()
    return s if s > 1e-9 else 1e-9
def above_ma(c, i, m): return P[c][i] > P[c][i-m:i].mean()

# ---------- ranking functions: return ordered list of coins ----------
def rk_mom(i):      return sorted(good, key=lambda c: mom(c, i, 30), reverse=True)
def rk_riskadj(i):  return sorted(good, key=lambda c: mom(c, i, 30)/vol(c, i, 20), reverse=True)
def rk_blend(i):    return sorted(good, key=lambda c: 0.5*mom(c, i, 30)+0.5*mom(c, i, 90), reverse=True)
def rk_radjblend(i):return sorted(good, key=lambda c: (0.5*mom(c, i, 30)+0.5*mom(c, i, 90))/vol(c, i, 20), reverse=True)
def rk_skiptop(i):  return rk_mom(i)[1:]   # skip the single most-extended name (crash magnet)

# ---------- sizing functions: picks -> weight dict (sums to 1) ----------
def sz_eq(i, picks):     return {c: 1.0/len(picks) for c in picks}
def sz_invvol(i, picks):
    iv = {c: 1.0/vol(c, i, 20) for c in picks}; s = sum(iv.values())
    return {c: iv[c]/s for c in picks}
def sz_mom(i, picks):
    m = {c: max(mom(c, i, 30), 1e-6) for c in picks}; s = sum(m.values())
    return {c: m[c]/s for c in picks}

# ---------- regime functions: i -> gross scale 0..1 ----------
def rg_btc(i):    return 1.0 if above_ma("BTC", i, 100) else 0.0
def rg_slope(i):  # BTC above 100dMA AND that MA rising
    ma, map_ = P["BTC"][i-100:i].mean(), P["BTC"][i-110:i-10].mean()
    return 1.0 if (P["BTC"][i] > ma and ma > map_) else 0.0
def rg_eth(i):    # BTC AND ETH both above 100dMA (market, not just BTC)
    return 1.0 if (above_ma("BTC", i, 100) and ("ETH" in good and above_ma("ETH", i, 100))) else 0.0
def rg_breadth_thr(i):  # invest only if >40% of universe is above its 100dMA
    b = sum(1 for c in good if above_ma(c, i, 100))/len(good)
    return 1.0 if b >= 0.40 else 0.0
def rg_breadth_scale(i):  # continuous: scale gross by breadth (monthly-check style), floor 0 below 0.3
    b = sum(1 for c in good if above_ma(c, i, 100))/len(good)
    if not above_ma("BTC", i, 100): return 0.0
    return min(1.0, max(0.0, (b-0.3)/0.5))   # 0 at 30% breadth, 1.0 at 80%+

# ---------- optional gross gate overlays: (i, picks) -> extra multiplier ----------
def gate_none(i, picks): return 1.0
def gate_corr(i, picks):  # throttle when holdings are too correlated (concentration risk)
    if len(picks) < 2: return 1.0
    M = np.array([P[c][i-20:i+1] for c in picks]); R = M[:, 1:]/M[:, :-1]-1
    C = np.corrcoef(R); m = (C.sum()-len(picks))/(len(picks)*(len(picks)-1))
    if m <= 0.5: return 1.0
    if m >= 0.9: return 0.6
    return 1.0 - (m-0.5)/0.4*0.4

def make(rank_fn, size_fn, regime_fn, gate_fn=gate_none, k=K, posfilter=True):
    def w(i):
        g = regime_fn(i)
        if g <= 0: return {}
        picks = rank_fn(i)[:k]
        if posfilter: picks = [c for c in picks if mom(c, i, 30) > 0]
        if not picks: return {}
        sw = size_fn(i, picks); gg = g*gate_fn(i, picks)
        return {c: gg*sw[c] for c in picks}
    return w

# ---------- engine ----------
def run(wfn, a, b, every=1):
    eq = [1.0]; wp = {}; to_sum = 0.0; bars = 0
    for i in range(a+W, b-1):
        if (i-(a+W)) % every == 0: w = wfn(i)
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in good)
        ret = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in good if P[c][i] > 0)
        eq.append(eq[-1]*(1+ret-to*FEE)); wp = w; bars += 1; to_sum += to
    return np.array(eq), to_sum/YRS
def met(eq):
    r = eq[1:]/eq[:-1]-1
    cagr = ((eq[-1])**(365/len(eq))-1)*100 if eq[-1] > 0 else -100
    sh = r.mean()/r.std()*np.sqrt(365) if r.std() > 0 else 0
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()*100
    return cagr, sh, dd
def score(wfn, every=1):
    eqf, turn = run(wfn, 0, n, every); fl = met(eqf)
    h1 = met(run(wfn, 0, n//2, every)[0]); h2 = met(run(wfn, n//2, n, every)[0])
    return fl, min(h1[1], h2[1]), turn

BASE = make(rk_mom, sz_eq, rg_btc)
VARIANTS = [
    ("BASELINE  mom30 · equal · BTC-regime · daily", BASE, 1),
    ("-- ranking math --", None, 1),
    ("rank: risk-adjusted mom (mom/vol)",  make(rk_riskadj, sz_eq, rg_btc), 1),
    ("rank: multi-horizon 30+90d blend",   make(rk_blend, sz_eq, rg_btc), 1),
    ("rank: risk-adj + blend",             make(rk_radjblend, sz_eq, rg_btc), 1),
    ("rank: skip-the-top (#2..6)",         make(rk_skiptop, sz_eq, rg_btc), 1),
    ("-- sizing math --", None, 1),
    ("size: inverse-vol (risk parity)",    make(rk_mom, sz_invvol, rg_btc), 1),
    ("size: momentum-weighted",            make(rk_mom, sz_mom, rg_btc), 1),
    ("-- regime / breadth checks --", None, 1),
    ("regime: + MA-slope rising",          make(rk_mom, sz_eq, rg_slope), 1),
    ("regime: BTC & ETH both >100dMA",     make(rk_mom, sz_eq, rg_eth), 1),
    ("regime: breadth>40% gate",           make(rk_mom, sz_eq, rg_breadth_thr), 1),
    ("regime: breadth-scaled gross",       make(rk_mom, sz_eq, rg_breadth_scale), 1),
    ("-- gates / cadence --", None, 1),
    ("gate: correlation throttle",         make(rk_mom, sz_eq, rg_btc, gate_corr), 1),
    ("cadence: weekly rebalance",          BASE, 7),
    ("cadence: monthly rebalance",         BASE, 30),
    ("-- combos of the winners --", None, 1),
    ("COMBO: risk-adj rank + inv-vol size",make(rk_riskadj, sz_invvol, rg_btc), 1),
    ("COMBO: risk-adj + invvol + slope",   make(rk_riskadj, sz_invvol, rg_slope), 1),
    ("COMBO: risk-adj + invvol + breadth", make(rk_riskadj, sz_invvol, rg_breadth_scale), 1),
]
print(f"{len(good)} coins · {n} bars · {YRS:.1f}yr · fee {FEE*1e4:.0f}bps/turn\n")
print(f"{'variant':<46}{'CAGR':>6}{'Shrp':>6}{'maxDD':>7}{'wHalf':>7}{'turn/yr':>9}")
print("-"*81)
base = None
for lab, wfn, ev in VARIANTS:
    if wfn is None:
        print(f"\n{lab}"); continue
    fl, wh, turn = score(wfn, ev)
    if base is None: base = (fl[1], wh)
    d = ""
    if base and wfn is not BASE:
        ds, dw = fl[1]-base[0], wh-base[1]
        d = f"  Δsh{ds:+.2f} Δwh{dw:+.2f}"
    print(f"{lab:<46}{fl[0]:>+5.0f}%{fl[1]:>6.2f}{fl[2]:>6.0f}%{wh:>7.2f}{turn:>8.1f}x{d}")
print("-"*81)
print("wHalf = worst of the two 2.75yr halves' Sharpe (out-of-sample robustness). Δ vs baseline.")
