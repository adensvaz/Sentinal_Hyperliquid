"""The execution policy must LEARN, not merely run.

Every test here targets a specific way a self-improving execution layer fails in production:
  * it never explores, so it never discovers the better arm       -> test_discovers_*
  * it scores unfilled passive orders as free                     -> test_miss_must_be_charged
  * it over-reacts to a handful of observations                   -> test_prior_dominates_early
  * it keeps believing a regime that has ended                    -> test_forgets_stale_regime
  * it drifts silently                                            -> test_drift_alert_fires
  * it learns its way into something insane                       -> test_guardrail_*
A policy that passes these is one whose learning can be trusted to run unattended.
"""
import time

import pytest

from sentinel.execution.exec_policy import (ARMS, CROSS, PEG, POST, Cell, ExecutionPolicy,
                                            PRIOR_COST_BPS, realised_cost_bps)

DAY = 86_400.0


def _pol(**kw):
    kw.setdefault("seed", 7)          # deterministic Thompson draws
    return ExecutionPolicy(**kw)


# ----------------------------------------------------------------- context
def test_hour_buckets_track_real_liquidity():
    """13:00 UTC measured 1.94x the volume of the quiet hours, so the bucket must separate them."""
    p = _pol()
    noon = time.mktime(time.struct_time((2026, 6, 1, 13, 0, 0, 0, 152, 0))) - time.timezone
    dawn = time.mktime(time.struct_time((2026, 6, 1, 9, 0, 0, 0, 152, 0))) - time.timezone
    assert "us_open" in p.context(ts=noon)
    assert "europe" in p.context(ts=dawn)


def test_context_separates_spread_and_vol():
    p = _pol()
    a = p.context(spread_bps=1.0, daily_vol=0.01)
    b = p.context(spread_bps=20.0, daily_vol=0.20)
    assert a != b, "tight/calm and wide/volatile must not share a cell"


# ----------------------------------------------------------------- learning
def test_discovers_that_crossing_is_cheaper_when_it_truly_is():
    """THE test. The prior says POST is cheapest (1.0 vs 5.5 bps). Build a world where POST
    actually costs 12bps — a thin book where resting orders get picked off and then chased — and
    the policy must overturn its own prior from evidence alone."""
    p = _pol()
    ctx = p.context(spread_bps=12.0, daily_vol=0.09)
    truth = {POST: 12.0, CROSS: 4.0, PEG: 8.0}
    rng = __import__("random").Random(1)
    for _ in range(400):
        arm = p.choose(ctx)
        p.record(ctx, arm, rng.gauss(truth[arm], 2.0))
    means = {a: p.posterior(ctx, a)[0] for a in ARMS}
    assert means[CROSS] < means[POST], f"failed to learn CROSS is cheaper here: {means}"
    picks = [p.choose(ctx) for _ in range(200)]
    assert picks.count(CROSS) > 120, f"learned it but does not act on it: {picks.count(CROSS)}/200"


def test_keeps_posting_where_posting_really_is_cheaper():
    """The mirror case — it must not simply drift to crossing everywhere."""
    p = _pol()
    ctx = p.context(spread_bps=2.0, daily_vol=0.02)
    truth = {POST: -0.5, CROSS: 5.0, PEG: 2.0}      # deep book: resting earns the spread
    rng = __import__("random").Random(2)
    for _ in range(300):
        arm = p.choose(ctx)
        p.record(ctx, arm, rng.gauss(truth[arm], 1.5))
    picks = [p.choose(ctx) for _ in range(200)]
    assert picks.count(POST) > 130, f"abandoned a genuinely good passive fill: {picks.count(POST)}"


def test_explores_all_arms_before_committing():
    """Without exploration the policy would ride its prior forever and never learn anything.
    Thompson sampling must try every arm early, while the posteriors are still wide."""
    p = _pol()
    ctx = p.context()
    seen = set()
    for _ in range(60):
        arm = p.choose(ctx)
        seen.add(arm)
        p.record(ctx, arm, PRIOR_COST_BPS[arm])
    assert seen == set(ARMS), f"never tried {set(ARMS) - seen} — exploration is broken"


def test_learning_is_context_specific():
    """What it learns about a thin Asian-hours book must not contaminate the US open."""
    p = _pol()
    thin = p.context(spread_bps=15.0, daily_vol=0.10)
    deep = p.context(spread_bps=1.5, daily_vol=0.02)
    for _ in range(120):
        p.record(thin, POST, 15.0)
    assert p.posterior(thin, POST)[0] > 9.0
    assert p.posterior(deep, POST)[0] < 3.0, "leaked evidence across contexts"


# ----------------------------------------------------------------- the subtle one
def test_miss_must_be_charged_not_scored_free():
    """A passive order that misses still has to be completed, usually at a worse price. If a miss
    is recorded as 0 the policy concludes POST is free and never crosses again. This asserts the
    accounting the caller is required to do: record the COMPLETED cost of the parent decision."""
    p = _pol()
    ctx = p.context()
    # honest accounting: 60% fill at -1bp, 40% miss completed at +14bps
    for _ in range(200):
        p.record(ctx, POST, -1.0 if __import__("random").random() < 0.6 else 14.0)
    honest = p.posterior(ctx, POST)[0]
    q = _pol()
    for _ in range(200):                       # the naive version: misses booked as free
        q.record(ctx, POST, -1.0 if __import__("random").random() < 0.6 else 0.0)
    naive = q.posterior(ctx, POST)[0]
    assert honest > naive + 2.0, (
        "honest miss-accounting must look materially worse than pretending misses are free; "
        f"honest={honest:.2f} naive={naive:.2f}")


# ----------------------------------------------------------------- stability
def test_prior_dominates_early():
    """Three unlucky observations must not flip the policy. PRIOR_STRENGTH is what stops a
    cold cell from being hijacked by noise."""
    p = _pol()
    ctx = p.context()
    for _ in range(3):
        p.record(ctx, POST, 40.0)
    mean, _ = p.posterior(ctx, POST)
    assert mean < 20.0, f"three observations moved the posterior to {mean:.1f} — far too twitchy"


def test_forgets_stale_regime():
    """Microstructure drifts. Evidence from six months ago must not outweigh this week's."""
    p = _pol(half_life_days=30.0)
    ctx = p.context()
    t0 = time.time() - 180 * DAY
    for _ in range(200):                        # ancient history: posting was terrible
        p.record(ctx, POST, 20.0, now=t0)
    now = time.time()
    for _ in range(20):                         # this week: posting is fine
        p.record(ctx, POST, 0.0, now=now)
    mean, _ = p.posterior(ctx, POST, now=now)
    assert mean < 6.0, f"stale regime still dominating at {mean:.1f}bps"


def test_uncertainty_shrinks_only_with_evidence():
    p = _pol()
    ctx = p.context()
    _, se0 = p.posterior(ctx, CROSS)
    for _ in range(100):
        p.record(ctx, CROSS, 5.0)
    _, se1 = p.posterior(ctx, CROSS)
    assert se1 < se0 / 2, "posterior did not tighten as evidence accumulated"


# ----------------------------------------------------------------- safety
def test_guardrail_rejects_catastrophic_arm():
    p = _pol(max_cost_bps=25.0)
    ctx = p.context()
    for _ in range(300):
        p.record(ctx, POST, 25.0)
        p.record(ctx, PEG, 24.0)
    assert p.choose(ctx, allowed=(POST, PEG, CROSS)) == CROSS


def test_guardrail_clips_absurd_observations():
    """One fat-fingered print must not poison a cell."""
    p = _pol(max_cost_bps=25.0)
    ctx = p.context()
    p.record(ctx, POST, 100_000.0)
    assert p.posterior(ctx, POST)[0] < 10.0


def test_always_returns_a_valid_arm():
    p = _pol(max_cost_bps=0.0001)              # everything looks unacceptable
    assert p.choose(p.context()) in ARMS


def test_drift_alert_fires_and_stays_quiet_otherwise():
    p = _pol()
    ctx = p.context()
    for _ in range(150):
        p.record(ctx, CROSS, 5.0)
    assert p.drift_alert(ctx, CROSS, 5.2) is None
    msg = p.drift_alert(ctx, CROSS, 45.0)
    assert msg and "drift" in msg


# ----------------------------------------------------------------- plumbing
def test_cost_sign_convention():
    """Positive = we paid. A BUY above the mid and a SELL below it are both costs."""
    assert realised_cost_bps("BUY", 100.10, 100.0) == pytest.approx(10.0, abs=0.1)
    assert realised_cost_bps("SELL", 99.90, 100.0) == pytest.approx(10.0, abs=0.1)
    assert realised_cost_bps("BUY", 99.90, 100.0) == pytest.approx(-10.0, abs=0.1)
    assert realised_cost_bps("BUY", 100.0, 0.0) == 0.0


def test_survives_restart_with_memory_intact():
    """Learning that does not persist is not learning — the loop restarts on every deploy."""
    p = _pol()
    ctx = p.context()
    for _ in range(80):
        p.record(ctx, CROSS, 2.0)
    before = p.posterior(ctx, CROSS)[0]
    q = ExecutionPolicy.from_json(p.to_json(), seed=7)
    assert q.posterior(ctx, CROSS)[0] == pytest.approx(before, abs=1e-6)


def test_from_json_survives_garbage():
    assert ExecutionPolicy.from_json("not json").choose("x") in ARMS
    assert ExecutionPolicy.from_json("").choose("x") in ARMS


def test_cell_decay_is_monotonic():
    c = Cell(n=100.0, mean=5.0, m2=50.0, last_ts=time.time() - 90 * DAY)
    c.decay(time.time(), half_life_days=30.0)
    assert c.n < 15.0, "three half-lives should leave under 1/8 of the weight"
