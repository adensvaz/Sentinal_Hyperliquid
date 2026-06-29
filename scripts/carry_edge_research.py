"""Quant improvement research for the carry strategy.

The three structural weaknesses identified from the year-by-year backtest:
  (A) 2022/2023 FLAT: we traded for near-zero return when funding was thin.
      → FIX: FUNDING REGIME GATE — only run when avg universe funding > threshold.
             No carry to harvest = no position. Protect capital, wait for the premium to return.

  (B) 37% DRAWDOWN (2021): classic short-squeeze — a high-funding coin ripped violently,
      our short bled hard before reverting.
      → FIX: RISK-PARITY SIZING — size inversely to each coin's recent volatility.
             A volatile coin (10%/day) gets half the weight of a stable one (5%/day).
             Equal-weight in carry is dangerous: you're equally exposed to a DOGE squeeze
             and a quiet stablecoin carry.

  (C) SIGNAL QUALITY: we rank by trailing-avg funding level. But funding has MORE information:
      the DIRECTION of funding change matters. A coin whose funding is RISING is a stronger
      short candidate than one whose funding is fading. A falling funding signal is a false short.
      → FIX: FUNDING MOMENTUM — require funding to be trending up to initiate/hold a short.
             This is cross-sectional momentum applied to the carry signal itself.

Tests each fix in isolation, then the best combination, then the FULL combo.
All through the live vol-target/DD-throttle overlay. Per-year + full period + bootstrap.
"""
import sys, httpx, time as _t, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
rng = np.random.default_rng(7)
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ",
         "TRX","FTM","MANA","AXS","EGLD","CHZ","ENJ","DASH","WAVES","COMP","SUSHI","GALA"]
DAY = 86400000
def klines(c, end=None):
    p = {"symbol": c+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    try: return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=25).json()
    except Exception: return []
def price_hist(c):
    out, end = {}, None
    for _ in range(5):
        rows = klines(c, end)
        if not isinstance(rows, list) or not rows: break
        for r in rows: out[int(r[0])//DAY] = float(r[4])
        end = int(rows[0][0])-1
        if len(rows) < 1000: break
    return (c, out) if len(out) > 200 else None
def fund_hist(c):
    out, start = {}, 1530000000000
    for _ in range(40):
        p = {"symbol": c+"USDT", "limit": 1000, "startTime": start}
        try: rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params=p, timeout=25).json()
        except Exception: break
        if not isinstance(rows, list) or not rows: break
        for x in rows:
            out.setdefault(int(x["fundingTime"])//DAY, 0.0)
            out[int(x["fundingTime"])//DAY] += float(x["fundingRate"])
        if len(rows) < 1000: break
        start = int(rows[-1]["fundingTime"])+1; _t.sleep(0.12)
    return (c, out) if len(out) > 150 else None
print("fetching data…")
PX = {c: d for c, d in (r for r in pmap(price_hist, COINS, workers=10) if r)}
FD = {c: d for c, d in (r for r in pmap(fund_hist, [c for c in COINS if c in PX], workers=4) if r)}
coins = [c for c in PX if c in FD]
days = sorted(set().union(*[set(PX[c]) for c in coins]))
P = {c: np.array([PX[c].get(d, np.nan) for d in days], float) for c in coins}
F = {c: np.array([FD[c].get(d, np.nan) for d in days], float) for c in coins}
av = {c: (~np.isnan(P[c])) & (P[c] > 0) & (~np.isnan(F[c])) for c in coins}
ncoin = np.array([sum(av[c][i] for c in coins) for i in range(len(days))])
start = int(np.argmax(ncoin >= 8))
days = days[start:]; n = len(days)
for c in coins: P[c], F[c], av[c] = P[c][start:], F[c][start:], av[c][start:]
YRS = n/365.0; START = 10_000.0; K=5; FEE=0.0008
YEAR = np.array([datetime.fromtimestamp(d*DAY/1000, tz=timezone.utc).year for d in days])

def avgF(c, i, look=5):
    a = F[c][max(0, i-look+1):i+1]; a = a[~np.isnan(a)]
    return a.mean() if len(a) else np.nan
def recentVol(c, i, k=20):
    closes = P[c][max(0,i-k):i+1]; r = closes[1:]/closes[:-1]-1
    r = r[~np.isnan(r)]; return r.std() if len(r)>2 else 0.02
def fundMom(c, i, short=5, long=15):
    """Funding momentum: recent avg vs longer avg. Positive = funding rising."""
    s = avgF(c, i, short); l = avgF(c, i, long)
    if np.isnan(s) or np.isnan(l) or l == 0: return 0.0
    return s - l                      # positive = funding accelerating = stronger carry signal
def mom(c, i, lb=21):
    if i-lb < 0 or not av[c][i] or not av[c][i-lb] or P[c][i-lb]<=0: return 0.0
    return P[c][i]/P[c][i-lb]-1

# ── engines ─────────────────────────────────────────────────────────────────────────────────────
def run_carry(funding_gate=0.0, risk_parity=False, fund_momentum=False):
    """Baseline + optional improvements.
    funding_gate:   min avg universe funding required to trade (e.g. 0.01% = 0.0001).
                    Below this threshold → hold cash. Kills the flat 2022/23 grind.
    risk_parity:    size inversely to each coin's recent volatility. Kills the squeeze tail.
    fund_momentum:  only short coins whose funding is RISING; filter out fading signals.
    """
    rets, tos, invested = [], [], []; wp = {}
    for i in range(21, n-1):
        elig = [c for c in coins if av[c][i] and av[c][i+1] and not np.isnan(avgF(c, i))]
        # ── IMPROVEMENT A: funding regime gate ────────────────────────────────────────────────
        if funding_gate > 0:
            universe_avg = np.mean([avgF(c, i) for c in elig]) if elig else 0.0
            if universe_avg < funding_gate:
                # funding thin → cash; no carry premium to harvest
                tos.append(sum(abs(wp.get(c, 0)) for c in coins)); rets.append(0.0)
                invested.append(0); wp = {}; continue
        if len(elig) < 12: w = {}
        else:
            # ── IMPROVEMENT C: funding momentum filter ────────────────────────────────────────
            if fund_momentum:
                # only include coins where funding is rising (strengthening signal)
                elig = [c for c in elig if fundMom(c, i) >= 0]
                if len(elig) < 12:
                    tos.append(sum(abs(wp.get(c, 0)) for c in coins))
                    rets.append(0.0); invested.append(0); wp = {}; continue
            rank = sorted(elig, key=lambda c: avgF(c, i))   # asc: low/neg first → long
            longs = rank[:K]; shorts = rank[-K:]
            # ── IMPROVEMENT B: risk-parity sizing ────────────────────────────────────────────
            if risk_parity:
                def rp_weights(group):
                    vols = {c: max(recentVol(c, i), 1e-4) for c in group}
                    inv_v = {c: 1.0/vols[c] for c in group}; s = sum(inv_v.values())
                    return {c: inv_v[c]/s for c in group}
                wl = rp_weights(longs); ws = rp_weights(shorts)
                w = {c: 0.5*wl[c] for c in longs}
                for c in shorts: w[c] = w.get(c, 0) - 0.5*ws[c]
            else:
                w = {c: 1.0/K for c in longs}
                for c in shorts: w[c] = w.get(c, 0) - 1.0/K
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in coins)
        pr = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in coins if w.get(c) and av[c][i] and av[c][i+1])
        fn = sum(-w.get(c, 0)*F[c][i+1] for c in coins if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); tos.append(to); invested.append(1 if w else 0); wp = w
    return np.array(rets), np.array(tos), np.array(invested)

def overlay(rets, tos):
    eq = START; peak = eq; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak; gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1+g*rets[t]-g*tos[t]*FEE); peak = max(peak, eq); out.append(eq); rv.append(g*rets[t])
    return np.array(out)

def metrics(eq):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2
    def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std()>0 else 0
    cagr = (eq[-1]/eq[0])**(1/YRS)-1
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    return cagr, sh(r), dd, min(sh(r[:h]), sh(r[h:]))

def per_year(eq, inv_arr, rets_arr):
    yr_days = YEAR[22:22+len(rets_arr)]
    prev = START; rows = {}
    for y in sorted(set(yr_days)):
        m = yr_days == y
        yr_eq = np.array([prev] + list(eq[1:][m]))
        ret = yr_eq[-1]/yr_eq[0]-1
        dd = ((np.maximum.accumulate(yr_eq)-yr_eq)/np.maximum.accumulate(yr_eq)).max()
        rows[y] = (ret*100, dd*100, inv_arr[m].mean()*100)
        prev = yr_eq[-1]
    return rows

# ── sweep ──────────────────────────────────────────────────────────────────────────────────────
VARIANTS = [
    ("BASELINE  (current)",               dict(funding_gate=0,      risk_parity=False, fund_momentum=False)),
    ("A) funding gate 0.005%/day",        dict(funding_gate=0.00005, risk_parity=False, fund_momentum=False)),
    ("A) funding gate 0.010%/day",        dict(funding_gate=0.0001,  risk_parity=False, fund_momentum=False)),
    ("A) funding gate 0.015%/day",        dict(funding_gate=0.00015, risk_parity=False, fund_momentum=False)),
    ("B) risk parity sizing",             dict(funding_gate=0,       risk_parity=True,  fund_momentum=False)),
    ("C) funding momentum filter",        dict(funding_gate=0,       risk_parity=False, fund_momentum=True)),
    ("A+B) gate + risk parity",           dict(funding_gate=0.0001,  risk_parity=True,  fund_momentum=False)),
    ("A+C) gate + fund momentum",         dict(funding_gate=0.0001,  risk_parity=False, fund_momentum=True)),
    ("B+C) risk parity + fund momentum",  dict(funding_gate=0,       risk_parity=True,  fund_momentum=True)),
    ("A+B+C) ALL THREE",                  dict(funding_gate=0.0001,  risk_parity=True,  fund_momentum=True)),
]
print(f"\n{len(coins)} coins · {YRS:.1f}yr · FEE={FEE*1e4:.0f}bps · $10k start\n")
print(f"{'variant':<38}{'CAGR':>6}{'Sharpe':>7}{'maxDD':>7}{'wHalf':>7}{'$final':>10}")
print("─"*75)
results = {}
for lab, kw in VARIANTS:
    rets, tos, inv = run_carry(**kw)
    eq = overlay(rets, tos)
    cagr, sh, dd, wh = metrics(eq)
    results[lab] = (eq, rets, inv)
    print(f"{lab:<38}{cagr*100:>+5.0f}%{sh:>7.2f}{dd*100:>6.0f}%{wh:>7.2f}   ${eq[-1]:>8,.0f}")

print("─"*75)
print("\nPER-YEAR return% | maxDD%  (A+B+C vs BASELINE)")
yr_base = per_year(*results["BASELINE  (current)"])
yr_best = per_year(*results["A+B+C) ALL THREE"])
print(f"{'year':<7}{'BASE ret':>9}{'BASE DD':>9}  │  {'BEST ret':>9}{'BEST DD':>9}{'invested%':>11}")
print("─"*56)
for y in sorted(yr_base):
    b = yr_base[y]; x = yr_best.get(y, b)
    print(f"{y:<7}{b[0]:>+8.0f}%{b[1]:>8.0f}%  │  {x[0]:>+8.0f}%{x[1]:>8.0f}%{x[2]:>10.0f}%")

print("\nBOOTSTRAP — A+B+C vs BASELINE (80 random 20-coin universes, full overlay)")
def bootstrap_one(kw, sub):
    rets, tos, inv = run_carry(**{**kw, **{"__sub": None}})  # reuse full run, filter results
    return None   # placeholder — we run sub-universes directly
def bt_sub(sub, kw):
    rets, tos, inv = [], [], []; wp = {}
    for i in range(21, n-1):
        elig = [c for c in sub if av[c][i] and av[c][i+1] and not np.isnan(avgF(c, i))]
        gate = kw.get("funding_gate", 0)
        if gate > 0:
            ua = np.mean([avgF(c, i) for c in elig]) if elig else 0.0
            if ua < gate:
                tos.append(sum(abs(wp.get(c, 0)) for c in sub)); rets.append(0.0); inv.append(0); wp = {}; continue
        if len(elig) < 8: w = {}
        else:
            if kw.get("fund_momentum"): elig = [c for c in elig if fundMom(c, i) >= 0]
            if len(elig) < 8: w = {}
            else:
                rank = sorted(elig, key=lambda c: avgF(c, i)); longs=rank[:3]; shorts=rank[-3:]
                if kw.get("risk_parity"):
                    def rw(g): vols={c:max(recentVol(c,i),1e-4) for c in g}; iv={c:1/v for c,v in vols.items()}; s=sum(iv.values()); return {c:iv[c]/s for c in g}
                    wl=rw(longs); ws=rw(shorts); w={c:0.5*wl[c] for c in longs}
                    for c in shorts: w[c]=w.get(c,0)-0.5*ws[c]
                else:
                    w={c:1/3 for c in longs};
                    for c in shorts: w[c]=w.get(c,0)-1/3
        to = sum(abs(w.get(c,0)-wp.get(c,0)) for c in sub)
        pr = sum(w.get(c,0)*(P[c][i+1]/P[c][i]-1) for c in sub if w.get(c) and av[c][i] and av[c][i+1])
        fn = sum(-w.get(c,0)*F[c][i+1] for c in sub if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); tos.append(to); inv.append(1 if w else 0); wp=w
    return overlay(np.array(rets), np.array(tos))
shs_base, shs_best, dds_base, dds_best = [], [], [], []
for _ in range(80):
    sub = list(rng.choice(coins, size=min(20, len(coins)), replace=False))
    if "BTC" not in sub: sub[0] = "BTC"
    cb, sb, db, wb = metrics(bt_sub(sub, {}))
    cx, sx, dx, wx = metrics(bt_sub(sub, {"funding_gate":0.0001,"risk_parity":True,"fund_momentum":True}))
    shs_base.append(sb); shs_best.append(sx); dds_base.append(db); dds_best.append(dx)
shs_base=np.array(shs_base); shs_best=np.array(shs_best); dds_base=np.array(dds_base); dds_best=np.array(dds_best)
print(f"BASELINE  Sharpe: mean {shs_base.mean():.2f} | maxDD mean {dds_base.mean()*100:.0f}%")
print(f"A+B+C     Sharpe: mean {shs_best.mean():.2f} | maxDD mean {dds_best.mean()*100:.0f}% | wins {100*(shs_best>shs_base).mean():.0f}% on Sharpe · {100*(dds_best<dds_base).mean():.0f}% on DD")
print("─"*75)
