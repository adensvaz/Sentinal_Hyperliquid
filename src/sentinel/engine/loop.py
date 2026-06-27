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


def next_daily_utc(hour: int, now: Optional[float] = None) -> float:
    """Epoch seconds of the next occurrence of `hour`:00:00 UTC (today if still ahead, else tomorrow)."""
    now = time.time() if now is None else now
    g = time.gmtime(now)
    target = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, int(hour) % 24, 0, 0, 0, 0, 0))
    while target <= now:
        target += 86400
    return float(target)


def run_loop(engine, minutes: int, on_report: Callable, monitor_seconds: int = 60,
             hour_utc: Optional[int] = None) -> None:
    monitor_s = max(10, monitor_seconds)
    fixed = hour_utc is not None
    rebalance_s = max(60, minutes * 60)

    if fixed:
        log.info("loop started: rebalance DAILY at %02d:00 UTC, safety check every %ds (%s mode). Ctrl-C to stop.",
                 int(hour_utc) % 24, monitor_s, engine.mode)
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
        next_rebalance = next_daily_utc(hour_utc)
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
            if time.time() >= next_rebalance:
                try:
                    on_report(engine.run_once())
                except Exception as e:
                    log.exception("rebalance cycle failed: %s", e)
                next_rebalance = next_daily_utc(hour_utc) if fixed else time.time() + rebalance_s
                try:
                    engine.store.set_meta("next_rebalance", int(next_rebalance))
                except Exception:
                    pass
                log.info("next rebalance at %s UTC", time.strftime("%Y-%m-%d %H:%M", time.gmtime(next_rebalance)))
    except KeyboardInterrupt:
        log.info("loop stopped by user.")
