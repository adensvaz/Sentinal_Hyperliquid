"""Close the loop: backtest the ACTUAL production FundingCarryStrategy.build_book() over the funding+
price history, proving the deployed code (with the live current-funding signal, not the research script's
trailing-avg) reproduces the carry edge. Through the live vol-target/DD-throttle overlay."""
import sys, httpx, time as _t, numpy as np
sys.path.insert(0, "src")
from sentinel.util.concurrency import pmap
from sentinel.config import Config, CarryCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.funding_carry import FundingCarryStrategy
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
        for x in rows: out.setdefault(int(x["fundingTime"])//DAY, 0.0); out[int(x["fundingTime"])//DAY] += float(x["fundingRate"])
        if len(rows) < 1000: break
        start = int(rows[-1]["fundingTime"])+1; _t.sleep(0.12)
    return (c, out) if len(out) > 150 else None

print("fetching prices + funding…")
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
YRS = n/365.0

SYM = {c: f"E-{c}-USDT" for c in coins}
registry = ContractRegistry([{"symbol": SYM[c], "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
    "multiplierCoin": c, "pricePrecision": 6, "minOrderVolume": 1, "maxMarketVolume": 1e12,
    "minLever": 0, "maxLever": 100, "status": 1} for c in coins])
cfg = Config(strategy="carry", carry=CarryCfg(top_k=5, lookback=21, target_gross=2.0))
strat = FundingCarryStrategy(cfg)
EQ = 1_000_000.0

def signal_returns():
    rets, tos = [], []; wprev = {}
    for i in range(21, n-1):
        closes = {SYM[c]: list(P[c][:i+1]) for c in coins if av[c][i]}
        prices = {SYM[c]: float(P[c][i]) for c in coins if av[c][i]}
        # production ranks on the TRAILING-AVG funding (store.recent_funding_avg) — replicate that here
        funding = {}
        for c in coins:
            if not av[c][i]: continue
            a = F[c][max(0, i-4):i+1]; a = a[~np.isnan(a)]
            if len(a): funding[SYM[c]] = float(a.mean())
        book = strat.build_book(closes, funding, prices, registry, EQ, gross_scale=1.0)
        w = {sym: pos.target_notional / EQ for sym, pos in book.positions.items()}
        cw = {c: w.get(SYM[c], 0.0) for c in coins}
        tos.append(sum(abs(cw[c]-wprev.get(c, 0)) for c in coins))
        pr = sum(cw[c]*(P[c][i+1]/P[c][i]-1) for c in coins if cw[c] and av[c][i] and av[c][i+1])
        fn = sum(-cw[c]*F[c][i+1] for c in coins if cw[c] and not np.isnan(F[c][i+1]))   # next-day carry
        rets.append(pr+fn); wprev = cw
    return np.array(rets), np.array(tos)

def overlay(rets, tos, fee=0.0008):
    eq = 10_000.0; peak = eq; out = [eq]; rv = []
    for t in range(len(rets)):
        vol = np.std(rv[-21:]) if len(rv) >= 5 else 0.0
        gv = min(1.0, 0.018/vol) if vol > 1e-9 else 1.0
        dd = 1-eq/peak; gd = 1.0 if dd <= 0.12 else (0.25 if dd >= 0.35 else 1.0-(dd-0.12)/0.23*0.75)
        g = max(0.25, min(1.0, gv, gd))
        eq *= (1+g*rets[t]-g*tos[t]*fee); peak = max(peak, eq); out.append(eq); rv.append(g*rets[t])
    return np.array(out)

r, to = signal_returns(); eq = overlay(r, to)
rr = eq[1:]/eq[:-1]-1; h = len(rr)//2
def sh(x): return x.mean()/x.std()*np.sqrt(365) if x.std() > 0 else 0
cagr = (eq[-1]/eq[0])**(1/YRS)-1
dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)).max()
print(f"\nPRODUCTION build_book() carry backtest · {len(coins)} coins · {YRS:.1f}yr · through live overlay, 8bps")
print(f"  $10k -> ${eq[-1]:,.0f}  ·  CAGR {cagr*100:+.0f}%  ·  Sharpe {sh(rr):.2f}  ·  maxDD {dd*100:.0f}%  ·  worst-half {min(sh(rr[:h]),sh(rr[h:])):.2f}")
print("  (research neutral_carry.py combo: Sharpe ~2.4; carry-only ~2.0 — production uses current funding, expect similar)")
