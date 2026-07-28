"""Per-book performance + current-situation report. Reads any Sentinel Store DB.

Usage:
  python scripts/book_report.py --config config.hl-trend.yaml [--marks]
  python scripts/book_report.py --db data/hl_trend.db --name Trend [--strategy trend] [--marks]

--marks fetches live Hyperliquid marks to compute open-position UPNL + mark-to-market equity
(only meaningful when the DB is LIVE — against a stale DB the marks are nonsense, so it's off by default).
"""
import argparse, sqlite3, time, json, urllib.request, datetime as dt, os

def q(con, sql, args=()):
    try: return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError: return []

def ago(ts):
    if not ts: return "—"
    h = (time.time() - float(ts)) / 3600.0
    return f"{h:.0f}h" if h < 48 else f"{h/24:.0f}d"

def hl_marks(syms):
    try:
        r = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(), headers={"Content-Type": "application/json"})
        m = json.load(urllib.request.urlopen(r, timeout=20))
        uni = m[0]["universe"]; ctx = m[1]
        return {uni[i]["name"]: float(ctx[i].get("markPx") or 0) for i in range(len(uni))}
    except Exception: return {}

def report(db, name, strategy, marks_on):
    if not os.path.exists(db):
        print(f"\n### {name:<16} — DB MISSING ({db}) — not present locally (lives on the VM)\n"); return
    mt = dt.datetime.fromtimestamp(os.path.getmtime(db)).strftime("%Y-%m-%d %H:%M")
    con = sqlite3.connect(db)
    modes = [r[0] for r in q(con, "SELECT DISTINCT mode FROM equity_curve")] or ["paper"]
    mode = max(modes, key=lambda mm: len(q(con, "SELECT 1 FROM equity_curve WHERE mode=?", (mm,))))
    acct = q(con, "SELECT starting_capital,realized_pnl,fees_paid,funding_pnl,peak_equity FROM account WHERE mode=?", (mode,))
    ec = q(con, "SELECT ts,equity,gross,net,drawdown_pct FROM equity_curve WHERE mode=? ORDER BY ts", (mode,))
    trades = q(con, "SELECT ts,symbol,side,entry,exit,contracts,pnl,duration_s FROM trades WHERE mode=? ORDER BY ts", (mode,))
    pos = q(con, "SELECT symbol,contracts,avg_price,multiplier,opened_ts FROM positions WHERE mode=? AND contracts!=0", (mode,))
    nfills = len(q(con, "SELECT 1 FROM fills WHERE mode=?", (mode,)))

    start, real, fees, fund, peakdb = (acct[0] if acct else (10000.0, 0, 0, 0, 0))
    start = start or 10000.0
    print(f"\n{'='*84}\n### {name.upper()}   (strategy={strategy}, mode={mode}, db@{mt})")
    if not ec:
        print("   no equity history yet.\n"); con.close(); return
    first_ts, last_ts = ec[0][0], ec[-1][0]
    eq_now = ec[-1][1]; span_d = (last_ts - first_ts) / 86400.0
    eqs = [r[1] for r in ec]; peak = max(peakdb or 0, max(eqs))
    maxdd = max([r[4] for r in ec] + [0.0])
    net = eq_now - start
    booked = (real or 0) + (fund or 0) - (fees or 0)
    print(f"   EQUITY  start ${start:,.0f} -> now ${eq_now:,.2f}   net {net:+,.2f} ({net/start*100:+.2f}%)"
          f"   peak ${peak:,.0f}  maxDD {maxdd:.2f}%")
    print(f"   BOOKED  realized {real:+,.2f}  funding {fund:+,.2f}  fees {-abs(fees or 0):,.2f}  = {booked:+,.2f}"
          f"   | span {span_d:.1f}d, {len(ec)} ticks, {nfills} fills")
    if strategy == "funding":
        print(f"   NOTE    delta-neutral: the real P&L is BOOKED ({booked:+,.2f}); open-leg MtM is hedged (~0)")

    # ---- closed trades ----
    if trades:
        pnls = [t[6] for t in trades]
        w = [p for p in pnls if p > 0]; l = [p for p in pnls if p < 0]
        pf = sum(w) / abs(sum(l)) if l else float('inf')
        avgh = sum(t[7] or 0 for t in trades) / len(trades) / 3600.0
        print(f"   TRADES  n={len(trades)}  win {100*len(w)/len(trades):.0f}%  "
              f"avgW ${sum(w)/len(w) if w else 0:,.1f}  avgL ${sum(l)/len(l) if l else 0:,.1f}  "
              f"PF {pf:.2f}  best ${max(pnls):,.1f}  worst ${min(pnls):,.1f}  total ${sum(pnls):,.1f}  avgHold {avgh:.0f}h")
        bysym = {}
        for t in trades: bysym.setdefault(t[1], []).append(t[6])
        rank = sorted(bysym.items(), key=lambda kv: sum(kv[1]))
        losers = [f"{s} ${sum(p):,.0f}({len(p)})" for s, p in rank[:3] if sum(p) < 0]
        winners = [f"{s} ${sum(p):,.0f}({len(p)})" for s, p in rank[::-1][:3] if sum(p) > 0]
        if winners: print(f"           top winners: {', '.join(winners)}")
        if losers:  print(f"           top losers : {', '.join(losers)}")
    else:
        print(f"   TRADES  none closed ({'carry book accrues, no round-trips' if strategy=='funding' else 'still holding / no exits yet'})")

    # ---- open positions ----
    if pos:
        mk = hl_marks([p[0] for p in pos]) if marks_on else {}
        rows = []; gross = 0.0; upnl_tot = 0.0
        for sym, c, entry, mult, ots in pos:
            mult = mult or 1.0; notl = abs(c) * entry * mult; gross += notl
            side = "LONG" if c > 0 else "SHORT"
            mark = mk.get(sym)
            up = (mark - entry) * c * mult if mark else None
            if up is not None: upnl_tot += up
            rows.append((notl, sym, side, c, entry, mark, up, ots))
        rows.sort(reverse=True)
        print(f"   OPEN    {len(pos)} positions, gross ${gross:,.0f}"
              + (f", live UPNL {upnl_tot:+,.2f}" + (" (phantom — hedged)" if strategy=='funding' else "") if marks_on else ""))
        for notl, sym, side, c, entry, mark, up, ots in rows[:20]:
            ms = f" mark {mark:,.4f} upnl {up:+,.1f}" if up is not None else ""
            print(f"           {sym:<7}{side:<6} {c:>10,d} @ {entry:<10,.4f} ${notl:>8,.0f}  {ago(ots):>4}{ms}")
    else:
        print(f"   OPEN    flat (no positions) — 100% cash")
    con.close()

def db_from_cfg(path):
    import yaml
    y = yaml.safe_load(open(path))
    return y["state"]["db_path"], y.get("strategy", "?"), (y.get("capital_usdt") or 10000)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config"); ap.add_argument("--db"); ap.add_argument("--name")
    ap.add_argument("--strategy", default="?"); ap.add_argument("--marks", action="store_true")
    a = ap.parse_args()
    if a.config:
        db, strat, _ = db_from_cfg(a.config)
        report(db, a.name or os.path.basename(a.config).replace("config.", "").replace(".yaml", ""), strat, a.marks)
    else:
        report(a.db, a.name or os.path.basename(a.db), a.strategy, a.marks)
