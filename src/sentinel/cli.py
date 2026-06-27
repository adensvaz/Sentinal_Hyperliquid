"""Command-line interface: selftest | once | loop | status | flatten."""
from __future__ import annotations

import argparse
import logging
from typing import Optional

from .config import load_config
from .engine.engine import Engine, RebalanceReport
from .engine.loop import run_loop
from .util.logging import setup_logging

log = logging.getLogger("sentinel")


def _console():
    try:
        from rich.console import Console
        return Console()
    except Exception:  # pragma: no cover
        return None


def print_report(report: RebalanceReport) -> None:
    c = _console()
    status = "PAUSED (drawdown kill-switch)" if report.paused else "OK"
    head = (f"{report.mode.upper()} | equity=${report.equity:,.2f} | "
            f"gross=${report.gross:,.0f} net=${report.net:,.0f} | dd={report.drawdown_pct:.2f}% | "
            f"universe={report.universe_size} | {report.n_long}L/{report.n_short}S | "
            f"orders={report.n_orders} | {status}")
    if c is None:
        print(head)
    else:
        c.print(f"[bold cyan]{head}[/]")

    if report.positions:
        try:
            from rich.table import Table
            t = Table(show_header=True, header_style="bold")
            for col in ("symbol", "side", "contracts", "notional($)", "score"):
                t.add_column(col)
            for p in sorted(report.positions, key=lambda x: x["score"], reverse=True):
                colour = "green" if p["side"] == "LONG" else "red"
                t.add_row(p["symbol"], f"[{colour}]{p['side']}[/]", str(p["contracts"]),
                          f"{p['notional']:,.0f}", f"{p['score']:+.2f}")
            c.print(t)
        except Exception:
            for p in report.positions:
                print(f"  {p['symbol']:14s} {p['side']:5s} {p['contracts']:>8d} "
                      f"${p['notional']:>10,.0f} score={p['score']:+.2f}")

    if report.fills:
        ok = sum(1 for f in report.fills if not str(f.status).startswith("ERROR"))
        errs = [f for f in report.fills if str(f.status).startswith("ERROR")]
        msg = f"fills: {ok} ok, {len(errs)} errors"
        (c.print(msg) if c else print(msg))
        for f in errs[:8]:
            line = f"  ERROR {f.symbol} {f.side} {f.open} vol={f.volume}: {f.status}"
            (c.print(f"[red]{line}[/]") if c else print(line))


def print_status(engine: Engine) -> None:
    c = _console()
    last_eq = engine.store.last_equity()
    peak = engine.store.peak_equity(last_eq or 0.0)
    positions = {}
    try:
        positions = engine.broker.positions()
    except Exception as e:
        log.warning("could not read positions: %s", e)
    dd = 0.0 if not (peak and last_eq) else max(0.0, (peak - last_eq) / peak * 100)
    head = (f"{engine.mode.upper()} | last equity=${(last_eq or 0):,.2f} | peak=${peak:,.2f} | "
            f"drawdown={dd:.2f}% | open positions={len(positions)}")
    (c.print(f"[bold]{head}[/]") if c else print(head))
    for sym, contracts in sorted(positions.items()):
        side = "LONG" if contracts > 0 else "SHORT"
        line = f"  {sym:14s} {side:5s} {contracts:>8d} contracts"
        (c.print(line) if c else print(line))
    trades = engine.store.recent_trades(10)
    if trades:
        (c.print("recent trades:") if c else print("recent trades:"))
        for t in trades:
            line = f"  {t['symbol']:14s} {t['side']:4s} {t['open']:5s} vol={t['volume']} @{t['price']} [{t['status']}]"
            (c.print(line) if c else print(line))


def refresh_whales(cfg) -> None:
    """Re-screen the Hyperliquid leaderboard (win-rate + recency gated) and rewrite the weekly basket."""
    from .exchange.client import KoinbayClient
    from .exchange.contracts import ContractRegistry
    from .exchange.futures import KoinbayFutures
    from .signal.whale_select import select_top_whales, write_basket

    kc = KoinbayClient(cfg.exchange.futures_host, timeout_s=cfg.exchange.timeout_s)
    try:
        kb_coins = {s.multiplier_coin.upper() for s in ContractRegistry(KoinbayFutures(kc).contracts()).active_usdt()}
    finally:
        kc.close()
    log.info("screening Hyperliquid leaderboard (win-rate + recency gated; ~1-2 min)...")
    picks = select_top_whales(kb_coins, info_url=cfg.whales.info_url,
                              max_inactive_days=cfg.whales.max_inactive_days, log=log)
    write_basket(cfg.whales.source_file, picks)
    log.info("wrote %d whales to %s", len(picks), cfg.whales.source_file)
    for p in picks:
        log.info("  %s  win=%.0f%%  30dPnL=$%.0f  active=%.1fd  q=%.2f",
                 p["address"][:12], p["win_rate"] * 100, p["month_pnl"], p["days_since"], p["q"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel",
                                description="Sentiment Edge-style market-neutral strategy on KoinBay futures")
    p.add_argument("command", choices=["selftest", "once", "loop", "status", "flatten",
                                       "dashboard", "refresh-whales", "backtest"])
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--live", action="store_true",
                   help="enable LIVE trading (also requires `mode: live` in config.yaml)")
    p.add_argument("--port", type=int, default=8787, help="dashboard port (default 8787)")
    p.add_argument("--no-open", action="store_true", help="dashboard: don't auto-open the browser")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.state.log_path)

    if args.command == "dashboard":
        from .dashboard.server import serve
        serve(cfg, port=args.port, open_browser=not args.no_open)
        return 0

    if args.command == "refresh-whales":
        refresh_whales(cfg)
        return 0

    if args.command == "backtest":
        from .backtest import run_cli
        run_cli(cfg)
        return 0

    live = False
    if args.command in ("once", "loop", "flatten"):
        if args.live:
            if cfg.mode != "live":
                log.error("--live given but config mode is '%s'. Set `mode: live` in %s to confirm real trading.",
                          cfg.mode, args.config)
                return 2
            cfg.require_keys()
            live = True
            log.warning("LIVE MODE: real orders will be placed on KoinBay futures.")
        elif cfg.mode == "live":
            log.warning("config mode is 'live' but --live not passed -> running PAPER. Add --live to trade for real.")

    # status reads the live account only if fully configured for live
    if args.command == "status":
        live = cfg.mode == "live" and bool(cfg.api_key and cfg.api_secret)

    engine = Engine(cfg, live=live)
    try:
        if args.command == "selftest":
            engine.selftest()
        elif args.command == "once":
            print_report(engine.run_once())
        elif args.command == "flatten":
            print_report(engine.flatten())
        elif args.command == "loop":
            run_loop(engine, cfg.schedule.rebalance_minutes, print_report, cfg.risk.monitor_seconds,
                     cfg.schedule.rebalance_hour_utc)
        elif args.command == "status":
            print_status(engine)
    finally:
        engine.close()
    return 0
