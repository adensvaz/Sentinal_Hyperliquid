import time

from sentinel.util.concurrency import pmap


def test_pmap_preserves_order():
    assert pmap(lambda x: x * x, [1, 2, 3, 4]) == [1, 4, 9, 16]


def test_pmap_empty():
    assert pmap(lambda x: x, []) == []


def test_pmap_failures_become_none():
    def f(x):
        if x == 2:
            raise ValueError("boom")
        return x
    assert pmap(f, [1, 2, 3]) == [1, None, 3]


def test_pmap_runs_concurrently():
    # 10 tasks each sleeping 0.1s should finish well under the 1.0s a serial run would take
    t0 = time.time()
    out = pmap(lambda x: (time.sleep(0.1), x)[1], list(range(10)), workers=10)
    assert out == list(range(10))
    assert time.time() - t0 < 0.6   # concurrency proven (serial would be ~1.0s)


def test_next_daily_utc():
    import calendar
    from sentinel.engine.loop import next_daily_utc
    # 2026-06-26 10:00:00 UTC, target hour 14 -> same day 14:00 UTC
    now = calendar.timegm((2026, 6, 26, 10, 0, 0, 0, 0, 0))
    assert next_daily_utc(14, now) == calendar.timegm((2026, 6, 26, 14, 0, 0, 0, 0, 0))
    # 2026-06-26 16:00 UTC, target 14 -> already passed -> next day
    now2 = calendar.timegm((2026, 6, 26, 16, 0, 0, 0, 0, 0))
    assert next_daily_utc(14, now2) == calendar.timegm((2026, 6, 27, 14, 0, 0, 0, 0, 0))
    # exactly at the hour -> rolls to next day (target <= now)
    now3 = calendar.timegm((2026, 6, 26, 14, 0, 0, 0, 0, 0))
    assert next_daily_utc(14, now3) == calendar.timegm((2026, 6, 27, 14, 0, 0, 0, 0, 0))
