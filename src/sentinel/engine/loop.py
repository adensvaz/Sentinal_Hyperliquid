"""Scheduler loop with TWO cadences:
  - every `monitor_seconds`: a lightweight safety check (drawdown kill-switch + per-position stop)
  - the full rebalance: either every `minutes` (interval mode) OR daily at a fixed UTC hour
    (`hour_utc`, predictable + restart-proof). The next-rebalance time is persisted to the store so
    the dashboard can show a live countdown.
Surviving per-iteration errors so the loop never dies on one bad cycle.
"""
from __future__ import annotations

import calendar
import logging
import time
from typing import Callable, Optional

log = logging.getLogger("sentinel")


def next_daily_utc(hour: int, now: Optional[float] = None, every_days: int = 1) -> float:
    """Epoch seconds of the next `hour`:00:00 UTC, stepping `every_days` at a time.

    A fixed hour used to imply DAILY, silently overriding rebalance_minutes. Trend asks for 2880
    minutes (2 days) — its own config notes the backtest at Sharpe 0.96 vs 0.79 for daily — and got
    daily anyway, re-ranking a 2-day trend signal twice as often as intended.
    """
    now = time.time() if now is None else now
    step = 86400 * max(1, int(every_days))
    g = time.gmtime(now)
    target = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, int(hour) % 24, 0, 0, 0, 0, 0))
    while target <= now:
        target += step
    return float(target)


def run_loop(engine, minutes: int, on_report: Callable, monitor_seconds: int = 60,
             hour_utc: Optional[int] = None) -> None:
    monitor_s = max(10, monitor_seconds)
    fixed = hour_utc is not None
    rebalance_s = max(60, minutes * 60)
    # A fixed hour pins WHEN in the day; rebalance_minutes still decides HOW OFTEN. Anything >= 2 days
    # becomes a day stride, so 2880 means every second day at that hour rather than every day.
    stride = max(1, int(round(minutes / 1440.0))) if fixed else 1

    if fixed:
        log.info("loop started: rebalance every %d day(s) at %02d:00 UTC, safety check every %ds (%s mode). "
                 "Ctrl-C to stop.", stride, int(hour_utc) % 24, monitor_s, engine.mode)
    else:
        log.info("loop started: rebalance every %d min, safety check every %ds (%s mode). Ctrl-C to stop.",
                 minutes, monitor_s, engine.mode)

    # Bootstrap: rebalance immediately ONLY if there's no book yet (a fresh account). On a restart with
    # existing positions we DON'T re-trade — we wait for the next scheduled time. This is what stops a
    # deploy/restart from churning the book.
    try:
        has_book = bool(engine.store.load_positions(engine.mode))
    except Exception:
        has_book = False

    if fixed:
        if not has_book:
            try:
                on_report(engine.run_once())
            except Exception as e:
                log.exception("initial rebalance failed: %s", e)
        next_rebalance = next_daily_utc(hour_utc, every_days=stride)
    else:
        try:
            on_report(engine.run_once())
        except Exception as e:
            log.exception("initial rebalance failed: %s", e)
        next_rebalance = time.time() + rebalance_s

    try:
        engine.store.set_meta("next_rebalance", int(next_rebalance))
    except Exception:
        pass
    log.info("next rebalance at %s UTC", time.strftime("%Y-%m-%d %H:%M", time.gmtime(next_rebalance)))

    try:
        while True:
            time.sleep(monitor_s)
            try:
                r = engine.risk_check()
                if r.get("action") not in (None, "none"):
                    log.warning("safety rail fired: %s", r)
            except Exception as e:
                log.warning("safety check failed: %s", e)
            # The regime brake is a state, not a schedule: if it moves between cycles the book
            # should follow it rather than wait out the rest of the day. See
            # Engine.regime_gate_changed for why, and for what evidence does and does not support it.
            early = False
            try:
                early = engine.regime_gate_changed()
            except Exception as e:
                log.warning("regime gate check failed: %s", e)
            if time.time() >= next_rebalance or early:
                try:
                    on_report(engine.run_once())
                except Exception as e:
                    log.exception("rebalance cycle failed: %s", e)
                next_rebalance = next_daily_utc(hour_utc, every_days=stride) if fixed else time.time() + rebalance_s
                try:
                    engine.store.set_meta("next_rebalance", int(next_rebalance))
                except Exception:
                    pass
                log.info("next rebalance at %s UTC", time.strftime("%Y-%m-%d %H:%M", time.gmtime(next_rebalance)))
    except KeyboardInterrupt:
        log.info("loop stopped by user.")
