"""Bounded parallel map for I/O-bound fan-out (concurrent KoinBay REST fetches).

The exchange has no bulk ticker/index endpoint, so a rebalance must fan out ~130 ticker + ~48
klines/index GETs. Done serially that dominates wall-clock (~30s). The shared httpx.Client is
thread-safe and pools connections, so a small thread pool collapses that to a few seconds.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, Optional, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def pmap(fn: Callable[[T], R], items: Iterable[T], workers: int = 10) -> List[Optional[R]]:
    """Apply `fn` to each item concurrently (I/O-bound), preserving input order. A failing item
    yields None in its slot — callers already filter failures — so one bad symbol never aborts the
    batch. Workers are bounded to keep within exchange rate limits (the client retries 429s)."""
    items = list(items)
    if not items:
        return []

    def _safe(x: T) -> Optional[R]:
        try:
            return fn(x)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(_safe, items))
