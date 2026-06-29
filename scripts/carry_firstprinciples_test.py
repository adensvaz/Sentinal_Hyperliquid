"""First-principles carry improvements — rigorous test at PRODUCTION gross (±1.0/sleeve = target_gross 2.0),
production combo signal (carry_rank), live vol-target/DD-throttle overlay.

Ideas, derived from the economics (carry = selling insurance to over-levered crowd; premium == squeeze risk):
  L) LIQUIDITY FLOOR: restrict the tradable universe to the top-N most liquid coins (by trailing $ volume).
     First principles: the squeeze tail is a LIQUIDITY event — shorting illiquid degen coins for fat funding
     is pennies in front of a steamroller. Liquid coins = guard-railed carry + far more capacity (copy-trade).
  W) FUNDING-MAGNITUDE WEIGHTING: weight each short by how much funding it pays (capped), not equal-weight.
     First principles: collect more premium where it's richest. Counter-risk: richest funding = most crowded.

Tail-focused metrics: maxDD, WORST SINGLE DAY, WORST WEEK (the squeeze shows here, not in Sharpe).
Full path + per-year + bootstrap (does it hold across coin-universes?).
"""
import sys, httpx, time as _t, numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
from sentinel.strategy.funding_carry import carry_rank
rng = np.random.default_rng(17)
COINS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","BCH","ATOM","NEAR",
         "XLM","ETC","FIL","AAVE","UNI","ICP","ALGO","VET","HBAR","SAND","THETA","EOS","XTZ","INJ",
         "TRX","FTM","MANA","AXS","EGLD","CHZ","ENJ","DASH","WAVES","COMP","SUSHI","GALA"]
DAY = 86400000
def kl(c, end=None):
    p = {"symbol": c+"USDT", "interval": "1d", "limit": 1000}
    if end: p["endTime"] = end
    try: return httpx.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=25).json()
    except Exception: return []
def ph(c):
    """Returns close AND quote-volume per day."""
    cl, vol, end = {}, {}, None
    for _ in range(5):
        rows = kl(c, end)
        if not isinstance(rows, list) or not rows: break
        for r in rows:
            d = int(r[0])//DAY; cl[d] = float(r[4]); vol[d] = float(r[7])   # r[7] = quote (USDT) volume
        end = int(rows[0][0])-1
        if len(rows) < 1000: break
    return (c, cl, vol) if len(cl) > 200 else None
def fh(c):
    out, s = {}, 1530000000000
    for _ in range(40):
        p = {"symbol": c+"USDT", "limit": 1000, "startTime": s}
        try: rows = httpx.get("https://fapi.binance.com/fapi/v1/fundingRate", params=p, timeout=25).json()
        except Exception: break
        if not isinstance(rows, list) or not rows: break
        for x in rows: out.setdefault(int(x["fundingTime"])//DAY, 0.0); out[int(x["fundingTime"])//DAY] += float(x["fundingRate"])
        if len(rows) < 1000: break
        s = int(rows[-1]["fundingTime"])+1; _t.sleep(0.12)
    return (c, out) if len(out) > 150 else None
print("fetching prices+volume+funding…")
PR = {c: (cl, vol) for c, cl, vol in (r for r in pmap(ph, COINS, workers=10) if r)}
FD = {c: d for c, d in (r for r in pmap(fh, [c for c in COINS if c in PR], workers=4) if r)}
coins = [c for c in PR if c in FD]
days = sorted(set().union(*[set(PR[c][0]) for c in coins]))
P = {c: np.array([PR[c][0].get(d, np.nan) for d in days], float) for c in coins}
V = {c: np.array([PR[c][1].get(d, np.nan) for d in days], float) for c in coins}
F = {c: np.array([FD[c].get(d, np.nan) for d in days], float) for c in coins}
av = {c: (~np.isnan(P[c])) & (P[c] > 0) & (~np.isnan(F[c])) for c in coins}
nc = np.array([sum(av[c][i] for c in coins) for i in range(len(days))])
s0 = int(np.argmax(nc >= 8)); days = days[s0:]; n = len(days)
for c in coins: P[c], V[c], F[c], av[c] = P[c][s0:], V[c][s0:], F[c][s0:], av[c][s0:]
YRS = n/365.0; FEE = 0.0008
YEAR = np.array([datetime.fromtimestamp(d*DAY/1000, tz=timezone.utc).year for d in days])
def avgF(c, i, k=5):
    a = F[c][max(0, i-k+1):i+1]; a = a[~np.isnan(a)]; return a.mean() if len(a) else np.nan
def mom(c, i, lb=21):
    if i-lb < 0 or not av[c][i] or not av[c][i-lb] or P[c][i-lb] <= 0: return np.nan
    return P[c][i]/P[c][i-lb]-1
def liq(c, i, k=20):
    a = V[c][max(0, i-k+1):i+1]; a = a[~np.isnan(a)]; return a.mean() if len(a) else 0.0

def run(sub, top_liq=0, fund_weight=False, K=5):
    rets, tos = [], []; wp = {}
    for i in range(21, n-1):
        pool = [c for c in sub if av[c][i] and av[c][i+1]]
        # ── L: restrict to the top-N most liquid coins available today ──────────────────────
        if top_liq and len(pool) > top_liq:
            pool = sorted(pool, key=lambda c: liq(c, i), reverse=True)[:top_liq]
        cand = [(c, mom(c, i), avgF(c, i)) for c in pool
                if not np.isnan(mom(c, i)) and not np.isnan(avgF(c, i))]
        if len(cand) < 2*K: w = {}
        else:
            ranked = carry_rank(cand); L = ranked[:K]; S = ranked[-K:]
            fmap = {c: f for c, _, f in cand}
            def wt(g, by_funding):
                if by_funding:
                    raw = {c: max(abs(fmap[c]), 1e-6) for c in g}
                    # cap any single name at 40% of the sleeve
                    s = sum(raw.values()); wts = {c: raw[c]/s for c in g}
                    for _ in range(6):
                        over = [c for c in wts if wts[c] > 0.40+1e-9]
                        if not over: break
                        exc = sum(wts[c]-0.40 for c in over)
                        for c in over: wts[c] = 0.40
                        und = [c for c in wts if wts[c] < 0.40-1e-9]; b = sum(wts[c] for c in und)
                        if b <= 0: break
                        for c in und: wts[c] += exc*wts[c]/b
                    return wts
                return {c: 1.0/len(g) for c in g}
            wl = {c: 1.0/len(L) for c in L}                 # longs always equal-weight
            ws = wt(S, fund_weight)                          # shorts: equal or funding-weighted
            w = {c: wl[c] for c in L}
            for c in S: w[c] = w.get(c, 0) - ws[c]
        to = sum(abs(w.get(c, 0)-wp.get(c, 0)) for c in sub)
        pr = sum(w.get(c, 0)*(P[c][i+1]/P[c][i]-1) for c in sub if w.get(c) and av[c][i] and av[c][i+1])
        fn = sum(-w.get(c, 0)*F[c][i+1] for c in sub if w.get(c) and not np.isnan(F[c][i+1]))
        rets.append(pr+fn); tos.append(to); wp = w
    rets = np.array(rets); tos = np.array(tos)
    eq = 10000.0; peak = 10000.0; eql = [10000.0]; rv = []; net = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak; gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd)); dret = g*rets[t]-g*tos[t]*FEE
        eq *= (1+dret); peak = max(peak, eq); eql.append(eq); rv.append(g*rets[t]); net.append(dret)
    return np.array(eql), np.array(net)
def met(eq, net):
    r = eq[1:]/eq[:-1]-1; h = len(r)//2; sh = lambda x: x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
    cagr = (eq[-1]/eq[0])**(1/YRS)-1; dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
    worst_day = net.min()
    cs = np.cumprod(1+net); worst_wk = (cs[7:]/cs[:-7]-1).min() if len(net) > 7 else net.min()
    return cagr, sh(r), dd, worst_day, worst_wk

print(f"\n{len(coins)} coins · {YRS:.1f}yr · production gross · combo signal · tail-focused\n")
print(f"{'variant':<34}{'CAGR':>6}{'Shrp':>6}{'maxDD':>7}{'worstDay':>9}{'worstWk':>8}")
print("─"*70)
VAR = [
    ("BASELINE (all coins, equal-wt)",      dict(top_liq=0,  fund_weight=False)),
    ("L) top-20 liquid only",               dict(top_liq=20, fund_weight=False)),
    ("L) top-15 liquid only",               dict(top_liq=15, fund_weight=False)),
    ("L) top-12 liquid only",               dict(top_liq=12, fund_weight=False)),
    ("W) funding-weighted shorts",          dict(top_liq=0,  fund_weight=True)),
    ("L+W) top-15 liq + funding-weight",    dict(top_liq=15, fund_weight=True)),
]
res = {}
for lab, kw in VAR:
    eq, net = run(coins, **kw); res[lab] = (eq, net)
    c, sh, dd, wd, ww = met(eq, net)
    print(f"{lab:<34}{c*100:>+5.0f}%{sh:>6.2f}{dd*100:>6.0f}%{wd*100:>8.1f}%{ww*100:>7.1f}%")
print("─"*70)

print("\nPER-YEAR return%  (baseline vs top-15 liquid)")
def py(eq, net):
    yd = YEAR[22:22+len(net)]; prev = 10000.0; out = {}
    for y in sorted(set(yd)):
        m = yd == y; ye = np.array([prev]+list(eq[1:][m])); out[y] = (ye[-1]/ye[0]-1)*100; prev = ye[-1]
    return out
yb = py(*res["BASELINE (all coins, equal-wt)"]); yl = py(*res["L) top-15 liquid only"])
print(f"{'year':<7}{'baseline':>10}{'top-15 liq':>12}")
for y in sorted(yb): print(f"{y:<7}{yb[y]:>+9.0f}%{yl.get(y,0):>+11.0f}%")

print("\nBOOTSTRAP 60x (random 22-coin universes) — does liquidity-floor robustly cut the tail?")
def met_sub(sub, kw):
    eq, net = run(sub, K=3, **kw); return met(eq, net)
bs, dl = {}, {}
keys = [("baseline", dict(top_liq=0, fund_weight=False)), ("top-15 liq", dict(top_liq=15, fund_weight=False)),
        ("L+W", dict(top_liq=15, fund_weight=True))]
agg = {k: {"sh": [], "dd": [], "wd": []} for k, _ in keys}
for _ in range(60):
    sub = list(rng.choice(coins, size=22, replace=False))
    if "BTC" not in sub: sub[0] = "BTC"
    for k, kw in keys:
        c, sh, dd, wd, ww = met_sub(sub, kw)
        agg[k]["sh"].append(sh); agg[k]["dd"].append(dd); agg[k]["wd"].append(wd)
base = agg["baseline"]
for k, _ in keys:
    a = agg[k]; sh = np.array(a["sh"]); dd = np.array(a["dd"]); wd = np.array(a["wd"])
    if k == "baseline":
        print(f"  {k:<12} Sharpe {sh.mean():.2f} | maxDD {dd.mean()*100:.0f}% | worstDay {np.array(wd).mean()*100:.1f}%")
    else:
        bdd = np.array(base["dd"]); bwd = np.array(base["wd"]); bsh = np.array(base["sh"])
        print(f"  {k:<12} Sharpe {sh.mean():.2f} | maxDD {dd.mean()*100:.0f}% | worstDay {wd.mean()*100:.1f}%"
              f" | DD-wins {100*(dd<bdd).mean():.0f}% · tail-wins {100*(wd>bwd).mean():.0f}% · Sh-wins {100*(sh>bsh).mean():.0f}%")
print("─"*70)
print("ROBUST WIN = cuts maxDD & worst-day in most universes while Sharpe ~holds. Then it ships.")
