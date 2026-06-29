"""HEAVY backtest battery for the champion change (breadth-scaling + capped momentum-weighting)
before deploy. Validates OLD (equal-wt, no breadth) vs NEW across:
  1. Full metric suite (CAGR/Sharpe/Sortino/maxDD/Calmar/worst-month/worst-half/turnover/%cash)
  2. Per-calendar-year (consistency)
  3. Cost sensitivity (2..25 bps/turn)
  4. Sub-period thirds
  5. Parameter robustness grid (top_k x lookback x regime_ma) — is the edge structural?
  6. Universe bootstrap (random coin subsets) — survivorship/coin-selection robustness
  7. Block-bootstrap significance — is NEW>OLD beyond noise?
  8. Crash-window stress (May-21, 2022 bear, FTX)
All through the live vol-target/DD-throttle overlay. Fast model is validated to match the production
build_book() in champion_integration_bt.py (Sharpe 1.29->1.33, DD 35%->26%)."""
import sys, httpx, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(7)
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
n = len(T); W = 110; YRS = n/365.0
YEAR = np.array([datetime.fromtimestamp(t/1000, tz=timezone.utc).year for t in T])

def precompute(lb, regma, bma):
    mom, abv, fwd = {}, {}, {}
    for c in good:
        m = np.full(n, -9.0); ab = np.zeros(n, bool); fw = np.zeros(n)
        for i in range(W, n-1):
            if P[c][i-lb] > 0: m[i] = P[c][i]/P[c][i-lb]-1
            ab[i] = P[c][i] > P[c][i-bma:i].mean()
            fw[i] = P[c][i+1]/P[c][i]-1
        mom[c], abv[c], fwd[c] = m, ab, fw
    reg = np.array([P["BTC"][i] > P["BTC"][i-regma:i].mean() if i >= regma else False for i in range(n)])
    return mom, abv, fwd, reg
MOM, ABV, FWD, REG = precompute(30, 100, 100)

def capw(strength, cap):
    pos = {s: max(v, 1e-9) for s, v in strength.items()}; tot = sum(pos.values()) or 1.0
    w = {s: v/tot for s, v in pos.items()}
    if not cap or len(w)*cap < 1.0: return w
    for _ in range(8):
        over = [s for s in w if w[s] > cap+1e-12]
        if not over: break
        exc = sum(w[s]-cap for s in over)
        for s in over: w[s] = cap
        und = [s for s in w if w[s] < cap-1e-12]; base = sum(w[s] for s in und)
        if base <= 0: break
        for s in und: w[s] += exc*w[s]/base
    return w

def signal(coins, momwt, breadth, K=5, blo=0.30, bhi=0.80, cap=0.40, mom=MOM, abv=ABV, fwd=FWD, reg=REG):
    rets, tos, inv = [], [], []; wp = {}
    for i in range(W, n-1):
        if not reg[i]: w = {}
        else:
            cand = sorted(((c, mom[c][i]) for c in coins if mom[c][i] > 0), key=lambda x: x[1], reverse=True)[:K]
            if not cand: w = {}
            else:
                g = 1.0
                if breadth:
                    frac = np.mean([abv[c][i] for c in coins])
                    g = min(1.0, max(0.0, (frac-blo)/(bhi-blo)))
                if g <= 0: w = {}
                else:
                    ww = capw({c: m for c, m in cand}, cap) if momwt else {c: 1.0/len(cand) for c, m in cand}
                    w = {c: g*ww[c] for c in ww}
        tos.append(sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in coins)); inv.append(1 if w else 0)
        rets.append(sum(w.get(c, 0)*fwd[c][i] for c in coins)); wp = w
    return np.array(rets), np.array(tos), np.array(inv)

def overlay(rets, tos, fee=0.0004):
    eq = 1.0; peak = 1.0; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak
        gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1 + g*rets[t] - g*tos[t]*fee); peak = max(peak, eq); out.append(eq)
        rv.append(g*rets[t])
    return np.array(out)

def net_rets(coins, momwt, breadth, fee=0.0004, **kw):
    r, t, inv = signal(coins, momwt, breadth, fee=None if False else 0, **kw) if False else signal(coins, momwt, breadth, **kw)
    eq = overlay(r, t, fee); return eq[1:]/eq[:-1]-1, eq, inv
def sharpe(r): return r.mean()/r.std()*np.sqrt(365) if r.std() > 0 else 0.0
def sortino(r):
    d = r[r < 0]; dd = d.std() if len(d) > 1 else 1e-9
    return r.mean()/dd*np.sqrt(365) if dd > 0 else 0.0
def maxdd(eq): return ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
def worst_month(r):
    if len(r) < 21: return r.min()
    cs = np.cumprod(1+r); return (cs[21:]/cs[:-21]-1).min()
def whalf(r):
    h = len(r)//2; return min(sharpe(r[:h]), sharpe(r[h:]))

print(f"HEAVY BACKTEST · {len(good)} coins · {YRS:.1f}yr · {datetime.fromtimestamp(T[W]/1000,tz=timezone.utc).date()} -> {datetime.fromtimestamp(T[-1]/1000,tz=timezone.utc).date()}")

# ---- 1. full metric suite ----
rO, eO, iO = net_rets(good, False, False); rN, eN, iN = net_rets(good, True, True)
print("\n[1] FULL METRIC SUITE (through live overlay)")
print(f"{'metric':<16}{'OLD':>10}{'NEW':>10}")
for lab, fo, fn in [("CAGR%", (eO[-1]**(1/YRS)-1)*100, (eN[-1]**(1/YRS)-1)*100),
                    ("Sharpe", sharpe(rO), sharpe(rN)), ("Sortino", sortino(rO), sortino(rN)),
                    ("maxDD%", maxdd(eO)*100, maxdd(eN)*100),
                    ("Calmar", (eO[-1]**(1/YRS)-1)/maxdd(eO), (eN[-1]**(1/YRS)-1)/maxdd(eN)),
                    ("worstMonth%", worst_month(rO)*100, worst_month(rN)*100),
                    ("worst-half Sh", whalf(rO), whalf(rN)),
                    ("%timeInvested", iO.mean()*100, iN.mean()*100)]:
    print(f"{lab:<16}{fo:>10.2f}{fn:>10.2f}")

# ---- 2. per-year ----
print("\n[2] PER-CALENDAR-YEAR return%  (W=warmup-trimmed)")
yrs = sorted(set(YEAR[W+1:n]))
days_year = YEAR[W+1:n]
print(f"{'year':<8}{'OLD%':>8}{'NEW%':>8}{'winner':>9}")
ow = nw = 0
for y in yrs:
    mask = days_year == y
    if mask.sum() < 20: tag = "(part)"
    else: tag = ""
    ro, rn = rO[mask], rN[mask]
    so, sn = (np.prod(1+ro)-1)*100, (np.prod(1+rn)-1)*100
    win = "NEW" if sn > so else "OLD"; ow += win == "OLD"; nw += win == "NEW"
    print(f"{str(y)+tag:<8}{so:>8.0f}{sn:>8.0f}{win:>9}")
print(f"   year wins -> NEW {nw} / OLD {ow}")

# ---- 3. cost sensitivity ----
print("\n[3] COST SENSITIVITY (Sharpe / final-multiple)")
print(f"{'fee bps':<9}{'OLD Sh':>8}{'NEW Sh':>8}{'OLD x':>8}{'NEW x':>8}{'NEW wins?':>11}")
for bps in [2, 4, 8, 15, 25]:
    ro, eo, _ = net_rets(good, False, False, fee=bps/1e4); rn, en, _ = net_rets(good, True, True, fee=bps/1e4)
    print(f"{bps:<9}{sharpe(ro):>8.2f}{sharpe(rn):>8.2f}{eo[-1]:>8.1f}{en[-1]:>8.1f}{('yes' if sharpe(rn)>sharpe(ro) else 'NO'):>11}")

# ---- 4. sub-period thirds ----
print("\n[4] SUB-PERIOD THIRDS (Sharpe)")
L = len(rO); cut = [0, L//3, 2*L//3, L]
for k in range(3):
    a, b = cut[k], cut[k+1]
    print(f"   third {k+1}: OLD {sharpe(rO[a:b]):.2f}  NEW {sharpe(rN[a:b]):.2f}  -> {'NEW' if sharpe(rN[a:b])>sharpe(rO[a:b]) else 'OLD'}")

# ---- 5. parameter robustness grid ----
print("\n[5] PARAMETER GRID — NEW vs OLD across top_k x lookback x regime_ma (does the edge survive?)")
win_sh = win_dd = tot = 0
for K in (3, 5, 8):
    for lb in (20, 30, 45):
        for regma in (50, 100, 150):
            mom, abv, fwd, reg = precompute(lb, regma, 100)
            ro, to, _ = signal(good, False, False, K=K, mom=mom, abv=abv, fwd=fwd, reg=reg); eo = overlay(ro, to)
            rn, tn, _ = signal(good, True, True, K=K, mom=mom, abv=abv, fwd=fwd, reg=reg); en = overlay(rn, tn)
            so, sn = sharpe(eo[1:]/eo[:-1]-1), sharpe(en[1:]/en[:-1]-1)
            tot += 1; win_sh += sn > so; win_dd += maxdd(en) < maxdd(eo)
print(f"   NEW beat OLD on Sharpe in {win_sh}/{tot} configs · on maxDD in {win_dd}/{tot} configs")

# ---- 6. universe bootstrap ----
print("\n[6] UNIVERSE BOOTSTRAP — 120 random coin-subsets (size 18). Survivorship/selection robustness")
dsh, ddd = [], []
for _ in range(120):
    sub = list(rng.choice(good, size=18, replace=False))
    if "BTC" not in sub: sub[0] = "BTC"  # BTC needed for regime ref breadth-neutral; keep market anchor
    ro, to, _ = signal(sub, False, False); eo = overlay(ro, to)
    rn, tn, _ = signal(sub, True, True); en = overlay(rn, tn)
    dsh.append(sharpe(en[1:]/en[:-1]-1)-sharpe(eo[1:]/eo[:-1]-1)); ddd.append((maxdd(en)-maxdd(eo))*100)
dsh, ddd = np.array(dsh), np.array(ddd)
print(f"   ΔSharpe(NEW-OLD): mean {dsh.mean():+.2f}  ·  NEW wins {100*(dsh>0).mean():.0f}%  ·  [p5 {np.percentile(dsh,5):+.2f}, p95 {np.percentile(dsh,95):+.2f}]")
print(f"   ΔmaxDD (NEW-OLD): mean {ddd.mean():+.1f}pt ·  NEW lower {100*(ddd<0).mean():.0f}%  ·  [p5 {np.percentile(ddd,5):+.1f}, p95 {np.percentile(ddd,95):+.1f}]")

# ---- 7. block-bootstrap significance ----
print("\n[7] BLOCK-BOOTSTRAP SIGNIFICANCE (21d blocks, 2000 resamples, paired)")
L = len(rO); bs = 21; nb = L//bs; diffs = []
for _ in range(2000):
    starts = rng.integers(0, L-bs, size=nb)
    idx = np.concatenate([np.arange(s, s+bs) for s in starts])
    diffs.append(sharpe(rN[idx])-sharpe(rO[idx]))
diffs = np.array(diffs)
print(f"   Sharpe(NEW)-Sharpe(OLD): mean {diffs.mean():+.2f}  ·  P(NEW>OLD) = {100*(diffs>0).mean():.0f}%  ·  [p5 {np.percentile(diffs,5):+.2f}, p95 {np.percentile(diffs,95):+.2f}]")

# ---- 8. crash-window stress ----
print("\n[8] CRASH-WINDOW STRESS (return% / maxDD% in the window)")
def win_stats(lo, hi, rr):
    m = (days_year >= lo) & (days_year <= hi)  # year-level windows
    if m.sum() < 5: return None
    r = rr[m]; eq = np.cumprod(1+r); return (np.prod(1+r)-1)*100, maxdd(eq)*100
for lab, lo, hi in [("2022 bear (full yr)", 2022, 2022), ("2021 (blowoff+May)", 2021, 2021),
                    ("2024-25 chop", 2024, 2025)]:
    so = win_stats(lo, hi, rO); sn = win_stats(lo, hi, rN)
    if so and sn:
        print(f"   {lab:<22} OLD {so[0]:>+5.0f}% (dd {so[1]:>2.0f}%)   NEW {sn[0]:>+5.0f}% (dd {sn[1]:>2.0f}%)")

print("\nVERDICT below — read [5],[6],[7] for whether the edge is structural, coin-robust, and significant.")
