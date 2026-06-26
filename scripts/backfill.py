"""Backfill the paper dashboard from the CURRENT strategy's backtest (real KoinBay daily data),
from Jan 2026 -> now: equity curve + closed trades + fills + account. The live loop then continues
on top. Run: ./.venv/bin/python scripts/backfill.py"""
import sys
from datetime import datetime

sys.path.insert(0, "src")
from sentinel.config import load_config
from sentinel.backtest import load_history, simulate
from sentinel.state.store import Store

CUTOFF_MS = int(datetime(2026, 1, 1).timestamp() * 1000)

cfg = load_config()
cfg.signal.momentum_horizons = ["1day", "3day", "7day", "14day"]
maker = cfg.execution.maker
fee = 0.00025 if maker else 0.00075
slip = 2.0 if maker else cfg.execution.slippage_bps

hist = load_history(cfg, interval="1day", limit=300)
m = simulate(hist, cfg, rebalance_every=1, taker_fee=fee, slippage_bps=slip)
if "error" in m:
    print("backtest error:", m["error"]); sys.exit(1)

start_cap = 10_000.0
# rebase the curve so it STARTS at 10k on the Jan cutoff (the pre-Jan run just sets the universe warmup)
curve = [(ts, eq, gr) for ts, eq, gr in m["curve"] if ts >= CUTOFF_MS]
if not curve:
    print("no curve points after cutoff"); sys.exit(1)
base_eq = curve[0][1]
scale = start_cap / base_eq
curve = [(ts, eq * scale, gr * scale) for ts, eq, gr in curve]

trades = [t for t in m["bt_trades"] if int(t.get("closed_ts") or 0) >= CUTOFF_MS]
fills = [f for f in m["bt_fills"] if int(f[0]) >= CUTOFF_MS]

final_eq = curve[-1][1]
peak = start_cap
dd_series = []
for _ts, eq, _gr in curve:
    peak = max(peak, eq)
    dd_series.append(max(0.0, (peak - eq) / peak * 100.0) if peak else 0.0)
peak_eq = max(eq for _ts, eq, _gr in curve)

mode = cfg.mode
store = Store(cfg.state.db_path, cfg.state.equity_csv)
c = store.conn
for tbl in ("equity_curve", "trades", "fills", "scores", "positions"):
    c.execute(f"DELETE FROM {tbl} WHERE mode=?", (mode,))
c.execute("DELETE FROM account WHERE mode=?", (mode,))
c.commit()

# equity curve (ts seconds; gross/net per historical point not meaningful -> 0, dd computed)
c.executemany("INSERT INTO equity_curve(ts,mode,equity,gross,net,drawdown_pct) VALUES(?,?,?,?,?,?)",
              [(int(ts // 1000), mode, round(eq, 4), 0.0, 0.0, round(dd, 4))
               for (ts, eq, _gr), dd in zip(curve, dd_series)])

# closed trades (scale pnl to the rebased curve; contracts ~ notional/entry for display)
trows = []
for t in trades:
    entry = float(t["entry"]) or 1.0
    ntl = float(t.get("ntl", 0.0))
    ct = int(int(t["closed_ts"]) // 1000)
    ot = int(int(t.get("opened_ts", t["closed_ts"])) // 1000)
    trows.append((ct, mode, t["symbol"], t["side"], entry, float(t["exit"]),
                  max(1, int(ntl / entry)), float(t["pnl"]) * scale, ot, ct - ot))
c.executemany("INSERT INTO trades(ts,mode,symbol,side,entry,exit,contracts,pnl,opened_ts,duration_s) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)", trows)

# fills (tuple: ts, side, open_close, notional, price, symbol)
frows = []
for ts, side, oc, ntl, px, sym in fills:
    px = float(px) or 1.0
    frows.append((int(int(ts) // 1000), mode, sym, side, oc, max(1, int(float(ntl) / px)),
                  px, 0.0, "bt", "FILLED"))
c.executemany("INSERT INTO fills(ts,mode,symbol,side,open_close,volume,price,fee,order_id,status) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)", frows)
c.commit()

store.save_account(mode, starting_capital=start_cap, realized_pnl=final_eq - start_cap,
                   fees_paid=m["fees_total"] * scale, funding_pnl=0.0, last_funding_ts=0.0)
store.update_peak_equity(mode, peak_eq)
store.close()

months = sorted({datetime.fromtimestamp(ts // 1000).strftime("%Y-%m") for ts, _e, _g in curve})
print(f"BACKFILLED mode='{mode}' from {months[0]} -> {months[-1]}")
print(f"  equity points : {len(curve)}  ({start_cap:,.0f} -> {final_eq:,.2f})")
print(f"  net return    : {(final_eq/start_cap-1)*100:+.1f}%  over Jan->now")
print(f"  closed trades : {len(trows)}   fills: {len(frows)}")
print(f"  max drawdown  : {max(dd_series):.1f}%   peak {peak_eq:,.2f}")
