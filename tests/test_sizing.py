import pytest

from sentinel.strategy.sizing import (adv_caps, capped_weights, net_scales, normalize,
                                      sleeve_notionals, vol_adjust)


def test_vol_adjust_downweights_volatile_names():
    # same raw weight, but B is 2x as volatile -> B should get ~half the weight of A after capping
    raw = vol_adjust([1.0, 1.0], [0.02, 0.04])
    assert raw[0] == pytest.approx(2 * raw[1])   # inverse-vol


def test_vol_adjust_floor_guards_zero_vol():
    out = vol_adjust([1.0], [0.0], floor=1e-3)   # zero vol -> floored, not div-by-zero
    assert out[0] == pytest.approx(1000.0)


def test_vol_adjust_then_capped_equalizes_risk():
    # after vol-adjust + normalize, notional*vol (risk contribution) should be ~equal across names
    vols = [0.02, 0.05, 0.10]
    w = capped_weights(vol_adjust([1.0, 1.0, 1.0], vols), cap=0.9)
    risk = [wi * v for wi, v in zip(w, vols)]
    assert max(risk) - min(risk) < 1e-9          # equal risk contribution


def test_adv_caps_disabled_when_frac_zero():
    assert adv_caps({"A": 1e9}, {"A": 100.0}, 0.0) == {"A": 1.0}


def test_adv_caps_binds_only_above_threshold():
    sn = {"A": 5000.0, "B": 50.0}          # A is big, B is tiny
    adv = {"A": 40000.0, "B": 40000.0}     # cap = 5% * 40k = 2000
    sc = adv_caps(sn, adv, 0.05)
    assert sc["A"] == pytest.approx(2000.0 / 5000.0)   # A shrunk to the 2000 cap
    assert sc["B"] == 1.0                                # B well under -> untouched


def test_adv_caps_inert_at_small_size():
    # tiny book vs deep liquidity -> never binds (the realistic current-capital case)
    sn = {"A": 1500.0, "B": -1200.0}
    adv = {"A": 5_000_000.0, "B": 8_000_000.0}
    assert adv_caps(sn, adv, 0.05) == {"A": 1.0, "B": 1.0}


def test_adv_caps_unknown_adv_left_uncapped():
    assert adv_caps({"A": 1e9}, {}, 0.05) == {"A": 1.0}


def test_adv_then_net_clamp_compose():
    # mirrors the engine order: ADV cap shrinks a big name -> net drifts -> net clamp re-neutralizes
    sn = {"A": 9000.0, "B": -3000.0, "C": -3000.0}      # net +3000
    after_adv = {s: sn[s] * adv_caps(sn, {"A": 100000.0, "B": 1e9, "C": 1e9}, 0.05).get(s, 1.0) for s in sn}
    assert after_adv["A"] == pytest.approx(5000.0)       # A capped at 5% * 100k
    final = {s: after_adv[s] * net_scales(after_adv, 10000.0, 0.05).get(s, 1.0) for s in sn}
    assert abs(sum(final.values())) <= 0.05 * 10000 + 1e-6   # net back within the 5% rail


def test_normalize_sums_to_one():
    w = normalize([1, 2, 1])
    assert abs(sum(w) - 1.0) < 1e-9
    assert w == [0.25, 0.5, 0.25]


def test_normalize_all_zero_is_equal():
    assert normalize([0, 0, 0, 0]) == [0.25, 0.25, 0.25, 0.25]


def test_capped_weights_respects_cap_and_sums_to_one():
    w = capped_weights([10, 1, 1, 1], cap=0.4)
    assert abs(sum(w) - 1.0) < 1e-9
    assert max(w) <= 0.4 + 1e-9
    # the dominant name is capped, the rest absorb the excess
    assert w[0] == 0.4


def test_capped_weights_relaxes_infeasible_cap_to_equal():
    # cap 0.1 with 4 names is infeasible (4*0.1<1) -> equal weight
    w = capped_weights([5, 3, 2, 1], cap=0.1)
    assert w == pytest.approx([0.25, 0.25, 0.25, 0.25])


def test_sleeve_notionals_sign_and_magnitude():
    longs = sleeve_notionals([0.5, 0.5], sleeve_notional=1000, sign=+1)
    shorts = sleeve_notionals([0.5, 0.5], sleeve_notional=1000, sign=-1)
    assert longs == [500.0, 500.0]
    assert shorts == [-500.0, -500.0]


def _net(sn):
    return sum(sn.values())


def test_net_scales_noop_when_within_rail():
    sn = {"A": 1000.0, "B": -1020.0}      # net -20 on equity 10000 = 0.2% < 5%
    assert net_scales(sn, 10000.0, 0.05) == {"A": 1.0, "B": 1.0}


def test_net_scales_clamps_long_imbalance_to_rail():
    sn = {"A": 6000.0, "B": -4000.0}      # net +2000 = 20% of equity, rail 5%
    sc = net_scales(sn, 10000.0, 0.05)
    assert sc["A"] < 1.0 and sc["B"] == 1.0   # shrink the heavier (long) sleeve only
    scaled = {s: sn[s] * sc[s] for s in sn}
    assert _net(scaled) == pytest.approx(500.0)   # exactly 5% of 10000, sign preserved


def test_net_scales_clamps_short_imbalance_to_rail():
    sn = {"A": 4000.0, "B": -6000.0}      # net -2000 = -20%, rail 5%
    sc = net_scales(sn, 10000.0, 0.05)
    assert sc["B"] < 1.0 and sc["A"] == 1.0
    scaled = {s: sn[s] * sc[s] for s in sn}
    assert _net(scaled) == pytest.approx(-500.0)


def test_net_scales_only_tightens_and_safe_edges():
    sc = net_scales({"A": 6000.0, "B": -4000.0}, 10000.0, 0.05)
    assert all(v <= 1.0 for v in sc.values())
    # one-sided book: nothing to balance against -> no-op
    assert net_scales({"A": 1000.0, "B": 500.0}, 10000.0, 0.05) == {"A": 1.0, "B": 1.0}
    # zero equity -> safe no-op
    assert net_scales({"A": 1000.0, "B": -1000.0}, 0.0, 0.05) == {"A": 1.0, "B": 1.0}
