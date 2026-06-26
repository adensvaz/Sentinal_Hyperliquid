"""Scheduler loop with TWO cadences:
  - every `monitor_seconds`: a lightweight safety check (drawdown kill-switch + per-position stop)
  - every `minutes`:         a full rebalance (re-score + retrade)
Surviving per-iteration errors so the loop never dies on one bad cycle.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("sentinel")


def run_loop(engine, minutes: int, on_report: Callable, monitor_seconds: int = 60) -> None:
    rebalance_s = max(60, minutes * 60)
    monitor_s = max(10, monitor_seconds)
    log.info("loop started: rebalance every %d min, safety check every %ds (%s mode). Ctrl-C to stop.",
             minutes, monitor_s, engine.mode)
    try:
        on_report(engine.run_once())
    except Exception as e:
        log.exception("initial rebalance failed: %s", e)
    last_rebalance = time.time()
    try:
        while True:
            time.sleep(monitor_s)
            try:
                r = engine.risk_check()
                if r.get("action") not in (None, "none"):
                    log.warning("safety rail fired: %s", r)
            except Exception as e:
                log.warning("safety check failed: %s", e)
            if time.time() - last_rebalance >= rebalance_s:
                try:
                    on_report(engine.run_once())
                except Exception as e:
                    log.exception("rebalance cycle failed: %s", e)
                last_rebalance = time.time()
    except KeyboardInterrupt:
        log.info("loop stopped by user.")
