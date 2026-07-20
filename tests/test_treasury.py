"""Treasury (money manager) — deterministic unit tests."""
import types
from sentinel.portfolio.treasury import Treasury

DAY = 86400.0


def _cfg(enabled=True, frac=0.40, days=7.0):
    return types.SimpleNamespace(treasury=types.SimpleNamespace(
        enabled=enabled, sweep_frac=frac, sweep_interval_days=days))


def test_disabled_never_sweeps():
    t = Treasury(_cfg(enabled=False))
    r = t.maybe_sweep(12000, vault=0, hwm=10000, last_sweep_ts=0, now=100 * DAY)
    assert r.swept == 0.0
    assert r.vault == 0.0


def test_sweeps_40pct_of_new_profit_after_a_week():
    t = Treasury(_cfg())
    # seeded hwm=10000, last swept 1 day in, now is day 8 -> a week has passed
    # pool now 11000 -> new profit 1000 -> sweep 400
    r = t.maybe_sweep(11000, vault=0, hwm=10000, last_sweep_ts=1 * DAY, now=8 * DAY)
    assert abs(r.swept - 400.0) < 1e-6
    assert abs(r.vault - 400.0) < 1e-6
    # new hwm = post-sweep trading level = 11000 - 400 = 10600
    assert abs(r.hwm - 10600.0) < 1e-6


def test_no_sweep_before_a_week():
    t = Treasury(_cfg())
    # only 3 days since last sweep -> not due
    r = t.maybe_sweep(11000, vault=0, hwm=10000, last_sweep_ts=5 * DAY, now=8 * DAY)
    assert r.swept == 0.0
    assert r.checkpoint is False


def test_down_week_banks_nothing():
    t = Treasury(_cfg())
    # a week passed but the pool is below the high-water mark -> no sweep, clock resets
    r = t.maybe_sweep(9500, vault=500, hwm=10000, last_sweep_ts=1 * DAY, now=8 * DAY)
    assert r.swept == 0.0
    assert r.vault == 500.0          # vault untouched (never give back banked money)
    assert r.checkpoint is True      # checkpoint fired, just didn't sweep
    assert r.hwm == 10000.0          # baseline unchanged


def test_hwm_seeds_on_first_call():
    t = Treasury(_cfg())
    # first ever call: hwm=0 -> seeds to current pool, clock starts, no sweep yet
    r = t.maybe_sweep(10000, vault=0, hwm=0.0, last_sweep_ts=0.0, now=0.0)
    assert r.hwm == 10000.0


def test_compounding_sequence():
    """Two consecutive weekly sweeps ratchet the vault up and reset the baseline each time."""
    t = Treasury(_cfg())
    # week 1: 10000 -> 12000, sweep 40% of 2000 = 800, hwm -> 11200
    r1 = t.maybe_sweep(12000, vault=0, hwm=10000, last_sweep_ts=1 * DAY, now=8 * DAY)
    assert abs(r1.swept - 800) < 1e-6 and abs(r1.hwm - 11200) < 1e-6
    # week 2: pool now 12200 (trading = raw - vault). new profit vs hwm 11200 = 1000 -> sweep 400
    r2 = t.maybe_sweep(12200, vault=r1.vault, hwm=r1.hwm, last_sweep_ts=r1.last_sweep_ts, now=16 * DAY)
    assert abs(r2.swept - 400) < 1e-6
    assert abs(r2.vault - 1200) < 1e-6      # 800 + 400 banked total
