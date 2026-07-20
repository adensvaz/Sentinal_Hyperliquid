import pytest

from sentinel.risk.limits import (crash_guard, dispersion_scale, drawdown_breached, drawdown_pct,
                                  filter_by_funding, leverage_scale,
                                  net_exposure_ratio, net_within_tolerance,
                                  position_loss_breached, position_stop_breached, risk_scale)


def test_dispersion_scale_full_when_at_or_above_ref():
    assert dispersion_scale(0.03, 0.03, 0.5) == 1.0      # at ref
    assert dispersion_scale(0.05, 0.03, 0.5) == 1.0      # above ref -> never levers up


def test_dispersion_scale_shrinks_below_ref():
    assert dispersion_scale(0.015, 0.03, 0.5) == 0.5     # half the ref -> hits the floor
    assert dispersion_scale(0.024, 0.03, 0.5) == pytest.approx(0.8)   # proportional


def test_dispersion_scale_warmup_and_floor():
    assert dispersion_scale(0.02, 0.0, 0.5) == 1.0       # no reference yet -> no gate
    assert dispersion_scale(0.001, 0.05, 0.5) == 0.5     # collapse -> clamped to floor


def test_crash_guard_calm_market_is_full():
    closes = [100.0 + i * 0.1 for i in range(40)]   # steady grind up, no drawdown
    assert crash_guard(closes, dd_bars=30, bounce_bars=2, dd_trigger=0.15,
                       bounce_trigger=0.05, floor=0.5) == 1.0


def test_crash_guard_fires_on_bear_then_bounce():
    # fall 30% over ~30 bars to a low, then snap back +8% in 2 bars -> crash regime
    down = [100.0 * (1 - 0.30 * i / 30) for i in range(31)]   # 100 -> 70
    closes = down + [70.0 * 1.04, 70.0 * 1.08]                # +8% bounce off the low
    s = crash_guard(closes, dd_bars=30, bounce_bars=2, dd_trigger=0.15,
                    bounce_trigger=0.05, floor=0.5)
    assert 0.5 <= s < 1.0    # de-risked into the floor band


def test_crash_guard_no_fire_without_bounce():
    # deep drawdown but still falling (no snapback) -> momentum is WITH us, don't cut
    closes = [100.0 * (1 - 0.30 * i / 35) for i in range(36)]
    assert crash_guard(closes, dd_bars=30, bounce_bars=2, dd_trigger=0.15,
                       bounce_trigger=0.05, floor=0.5) == 1.0


def test_crash_guard_insufficient_history_is_full():
    assert crash_guard([100, 101, 102], dd_bars=30, bounce_bars=2,
                       dd_trigger=0.15, bounce_trigger=0.05, floor=0.5) == 1.0


def test_risk_scale_calm_no_drawdown_is_full():
    assert risk_scale([0.005, -0.004, 0.003], 0.0, 0.015, 0.25, 0.04, 0.12) == 1.0


def test_risk_scale_high_vol_derisks():
    s = risk_scale([0.03, -0.03, 0.03, -0.03], 0.0, 0.015, 0.25, 0.04, 0.12)  # vol .03 vs target .015
    assert 0.49 < s < 0.51


def test_risk_scale_deep_drawdown_floored():
    assert risk_scale([0.001, -0.001], 0.12, 0.015, 0.25, 0.04, 0.12) == 0.25


def test_risk_scale_mid_drawdown_halves():
    assert abs(risk_scale([0.001, -0.001], 0.08, 0.015, 0.25, 0.04, 0.12) - 0.5) < 1e-6


def test_leverage_scale_within_cap_is_one():
    assert leverage_scale(gross_notional=20_000, equity=10_000, max_gross_leverage=10) == 1.0


def test_leverage_scale_scales_down_over_cap():
    # gross 50k, equity 10k -> 5x; cap 3x -> scale to 30k/50k = 0.6
    assert leverage_scale(50_000, 10_000, 3) == 0.6


def test_net_exposure_and_tolerance():
    assert net_exposure_ratio(500, 10_000) == 0.05
    assert net_within_tolerance(500, 10_000, tol=0.05) is True
    assert net_within_tolerance(800, 10_000, tol=0.05) is False


def test_drawdown():
    assert drawdown_pct(100, 85) == 15.0
    assert drawdown_breached(100, 84, max_drawdown_pct=15) is True
    assert drawdown_breached(100, 86, max_drawdown_pct=15) is False


def test_position_loss_breached():
    # equity 10k, cap 6% -> breach if loss > $600
    assert position_loss_breached(-700, 10_000, 6) is True
    assert position_loss_breached(-500, 10_000, 6) is False
    assert position_loss_breached(+900, 10_000, 6) is False   # a gain never breaches
    assert position_loss_breached(-700, 10_000, 0) is False    # disabled


def test_position_stop_breached():
    # position entry notional $1,250, stop 30% -> breach if loss > $375
    assert position_stop_breached(-400, 1_250, 30) is True
    assert position_stop_breached(-300, 1_250, 30) is False
    assert position_stop_breached(+500, 1_250, 30) is False    # a gain never breaches
    assert position_stop_breached(-400, 1_250, 0) is False     # disabled
    assert position_stop_breached(-400, 0, 30) is False        # no notional -> can't breach


def test_ace_scenario_would_have_been_stopped():
    """The real ACE short: $1,280 entry notional, squeezed +89% -> ~-$1,140 loss.
    The old equity-based stop (12% of $10k = $1,200) did NOT fire (loss was $1,140 < $1,200).
    The new per-name stop (30% of $1,280 = $384) fires early, capping the disaster."""
    entry_notional = 1_280.0
    loss_at_89pct = -0.89 * entry_notional      # ~-$1,139
    # old stop: measured vs equity -> loss ($1,139) is just under the 12% line ($1,200), never fires
    assert position_loss_breached(loss_at_89pct, 10_000, 12) is False
    # new per-name stop: the position is down 89% of its own value -> far past the 30% stop -> FIRES.
    # In practice it exits the moment the loss first crosses 30% (~$384), capping the disaster there.
    assert position_stop_breached(loss_at_89pct, entry_notional, 30) is True
    assert position_stop_breached(-0.31 * entry_notional, entry_notional, 30) is True   # just past 30%
    assert position_stop_breached(-0.29 * entry_notional, entry_notional, 30) is False  # just under 30%


def test_funding_filter():
    funding = {"A": 0.0001, "B": 0.005, "C": -0.01, "D": 0.002}
    allowed = set(filter_by_funding(funding, abs_max=0.003))
    assert allowed == {"A", "D"}   # B and C exceed the band
