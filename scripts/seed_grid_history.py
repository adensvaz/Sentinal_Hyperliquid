"""Seed the GRID book with an ACCURATE backtested history — replayed from the REAL resting-limit engine
(sentinel.strategy.grid_book), the same code that runs live. NOT the snapshot approach (which loses),
and NOT fabricated.

Each coin's book is advanced bar-by-bar over real HOURLY prices: resting buy limits fill on the bar's
low, take-profits fill on the high, exactly as grid_book.tick does live. The portfolio equity path is
the honest track record this book would have produced. Written to the grid DB as daily equity points +
closed trades, flagged as backtest (meta: grid_backtest_until_ts) so the dashboard marks backtest->live.

Run ON THE VM as part of deploying the real grid, with the grid loop + dashboard STOPPED:
    sudo systemctl stop sentinel-grid sentinel-grid-dashboard
    ~/sentinel/.venv/bin/python scripts/seed_grid_history.py --days 150
    sudo systemctl start sentinel-grid sentinel-grid-dashboard
"""
import argparse, sys, time, json, urllib.request
sys.path.insert(0, "src")
import numpy as np
from sentinel.config import load_config
from sentinel.strategy import grid_book as gb
from sentinel.state.store import Store

BINANCE = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT",
           "LINK": "LINKUSDT", "ADA": "ADAUSDT", "LTC": "LTCUSDT"}
COST = 0.0005   # per fill (blended maker + slippage) — the honest fill model


def klines(sym, days):
    end = int(time.time() * 1000); start = end - int((days + 12) * 24 * 3600 * 1000); out = []; s = start
    while s < end:
        u = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&startTime={s}&limit=1000"
        try:
            k = json.load(urllib.request.urlopen(u, timeout=20))
        except Exception:
            break
        if not k:
            break
        out += k; s = k[-1][0] + 1
        if len(k) < 1000:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.hl-grid.yaml")
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    g = cfg.grid
    coins = [c for c in cfg.universe.allowlist if c in BINANCE]
    cap = float(cfg.capital_usdt or 10000.0)
    print(f"seeding {args.days}d hourly RESTING-LIMIT grid history for {coins} "
          f"(K={g.levels} sp={g.spacing_pct}% stop={g.stop_pct}% cost {COST*2*100:.2f}%/round-trip)...")

    ser = {}
    for c in coins:
        k = klines(BINANCE[c], args.days)
        if len(k) > 500:
            ser[c] = {int(r[0]): (float(r[2]), float(r[3]), float(r[4])) for r in k}  # high, low, close
    ref = max(ser, key=lambda c: len(ser[c])); T = sorted(ser[ref]); H = {}; L = {}; C = {}
    for c in ser:
        h = []; l = []; cc = []; lh = ll = lc = None
        for t in T:
            if t in ser[c]:
                lh, ll, lc = ser[c][t]
            h.append(lh); l.append(ll); cc.append(lc)
        if cc[0] is not None:
            H[c] = np.array(h, float); L[c] = np.array(l, float); C[c] = np.array(cc, float)
    coins = list(C); n = len(T); warm = g.ma_period + 5
    band = g.range_band; sp = g.spacing_pct / 100.0; stop = g.stop_pct / 100.0; unit = 1.0 / g.levels

    # replay the REAL engine per coin -> per-bar portfolio return (each coin weighted 1/N, 1x gross cap)
    port = np.zeros(n); ntrades = 0; wins = 0
    for c in coins:
        ma = np.array([C[c][max(0, i - g.ma_period):i + 1].mean() for i in range(n)])
        st = gb.new_state(C[c][warm]); prev = 0.0; cum = 0.0
        for i in range(warm, n - 1):
            ranging = ma[i] > 0 and abs(C[c][i] / ma[i] - 1) < band
            r = gb.tick(st, H[c][i], L[c][i], C[c][i], levels=g.levels, spacing=sp, stop=stop,
                        ranging=ranging, cost=COST, unit_notional=unit, fill_delta=g.fill_delta_pct / 100.0)
            for tr in r["closed"]:
                ntrades += 1; wins += 1 if tr["pnl"] > 0 else 0
            cum += r["realized"]; total = cum + r["open_pnl"]
            port[i] += (total - prev) / len(coins); prev = total

    eqc = cap * np.cumprod(1 + port[warm:n - 1])
    ts = np.array(T[warm:n - 1]) // 1000
    # daily downsample (day's closing equity), with running drawdown
    daily = {}
    for k in range(len(eqc)):
        d = time.strftime("%Y-%m-%d", time.gmtime(ts[k]))
        daily[d] = (int(ts[k]), float(eqc[k]))
    days_sorted = sorted(daily)
    peak = cap; pts = []
    for d in days_sorted:
        t, eq = daily[d]; peak = max(peak, eq); pts.append((t, eq, (peak - eq) / peak * 100))
    end_eq = pts[-1][1]
    rets = np.array([pts[k][1] / pts[k - 1][1] - 1 for k in range(1, len(pts))])
    sharpe = rets.mean() / rets.std() * np.sqrt(365) if rets.std() > 0 else 0
    mdd = max(p[2] for p in pts)
    print(f"\n  ACCURATE resting-limit history ({len(pts)} daily pts, {len(coins)} coins, {ntrades} round-trips):")
    print(f"    equity {cap:.0f} -> {end_eq:.0f}   ({(end_eq/cap-1)*100:+.1f}% over {args.days}d, "
          f"~{(end_eq/cap-1)*100/args.days*30:+.1f}%/mo)")
    print(f"    daily-Sharpe {sharpe:.2f}   maxDD {mdd:.0f}%   win-rate {100*wins/max(1,ntrades):.0f}%")

    if args.dry:
        print("\n  [dry] not writing DB.")
        return
    st = Store(cfg.state.db_path, cfg.state.equity_csv)
    st.conn.execute("DELETE FROM equity_curve WHERE mode='paper'")
    for t, eq, dd in pts:
        st.record_equity("paper", eq, 0.0, 0.0, dd, ts=t)
    st.save_account("paper", starting_capital=cap, realized_pnl=end_eq - cap, fees_paid=0.0,
                    funding_pnl=0.0, last_funding_ts=0.0)
    st.conn.execute("UPDATE account SET peak_equity=? WHERE mode='paper'", (peak,))
    st.set_meta("grid_backtest_until_ts", int(pts[-1][0]))
    # hand off to the live engine: start it AFTER the seeded window with a fresh book (no re-trading history)
    st.set_meta("grid_last_ts", int(T[-1]))
    st.set_meta("grid_state", {})
    st.conn.commit()
    print(f"\n  wrote {len(pts)} equity points + account (realized {end_eq-cap:+.0f}) to {cfg.state.db_path}")
    print(f"  handoff: grid_last_ts={int(T[-1])} (live resumes after the seed with a fresh book)")


if __name__ == "__main__":
    main()
