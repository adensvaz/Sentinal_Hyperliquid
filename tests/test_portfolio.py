"""Tests for the portfolio-construction robustness engine (beta-neutralization + indicators)."""
import math

import numpy as np
import pytest

from sentinel.strategy.portfolio import beta_scales, book_beta, compute_betas, demean_by_beta, dispersion


def _series(rets, start=100.0):
    """Build a close series from a list of simple returns."""
    px = [start]
    for r in rets:
        px.append(px[-1] * (1.0 + r))
    return px


def test_compute_betas_recovers_known_beta():
    rng = np.random.default_rng(0)
    btc = rng.normal(0, 0.02, 120)
    # ALT moves at 2x BTC plus small idiosyncratic noise -> beta ~2
    alt = 2.0 * btc + rng.normal(0, 0.001, 120)
    closes = {"E-BTC-USDT": _series(btc), "E-ALT-USDT": _series(alt)}
    betas = compute_betas(closes, "E-BTC-USDT", lookback=100)
    assert betas["E-BTC-USDT"] == pytest.approx(1.0, abs=1e-6)  # BTC vs itself
    assert betas["E-ALT-USDT"] == pytest.approx(2.0, rel=0.1)


def test_compute_betas_empty_when_ref_too_short():
    closes = {"E-BTC-USDT": [100, 101, 102], "E-X-USDT": [10, 11, 12]}
    assert compute_betas(closes, "E-BTC-USDT", lookback=30) == {}


def test_compute_betas_empty_when_ref_missing():
    closes = {"E-X-USDT": _series([0.01] * 50)}
    assert compute_betas(closes, "E-BTC-USDT", lookback=30) == {}


def test_book_beta_sign_and_scale():
    # long 1000 of beta-1.5, short 1000 of beta-0.5 -> net beta-weighted = 1000*1.5 - 1000*0.5 = 1000
    sn = {"A": 1000.0, "B": -1000.0}
    betas = {"A": 1.5, "B": 0.5}
    assert book_beta(sn, betas, equity=10000.0) == pytest.approx(0.10)  # 1000/10000


def test_book_beta_zero_equity_safe():
    assert book_beta({"A": 100.0}, {"A": 1.0}, equity=0.0) == 0.0


def test_beta_scales_neutralizes_heavier_long_sleeve():
    # long sleeve carries more beta than short sleeve -> longs get scaled down
    sn = {"A": 1000.0, "B": -1000.0}
    betas = {"A": 2.0, "B": 1.0}  # Lb=2000, Sb=1000
    scales = beta_scales(sn, betas, max_imbalance=0.15)
    assert scales["A"] < 1.0 and scales["B"] == 1.0
    # capped at 15% imbalance: can't shrink below 0.85 even though Sb/Lb=0.5
    assert scales["A"] == pytest.approx(0.85)
    # residual beta is reduced vs unscaled
    sn2 = {"A": sn["A"] * scales["A"], "B": sn["B"] * scales["B"]}
    assert abs(book_beta(sn2, betas, 1e4)) < abs(book_beta(sn, betas, 1e4))


def test_beta_scales_fully_neutralizes_within_cap():
    # mild imbalance the cap can fully absorb -> residual beta ~0
    sn = {"A": 1000.0, "B": -1000.0}
    betas = {"A": 1.1, "B": 1.0}  # Lb=1100, Sb=1000 -> f=0.909 (> 0.85 floor)
    scales = beta_scales(sn, betas, max_imbalance=0.15)
    sn2 = {s: sn[s] * scales[s] for s in sn}
    assert book_beta(sn2, betas, 1e4) == pytest.approx(0.0, abs=1e-9)


def test_beta_scales_shrinks_short_when_short_heavier():
    sn = {"A": 1000.0, "B": -1000.0}
    betas = {"A": 1.0, "B": 1.05}  # Sb heavier
    scales = beta_scales(sn, betas, max_imbalance=0.15)
    assert scales["B"] < 1.0 and scales["A"] == 1.0


def test_beta_scales_noop_when_one_sided():
    sn = {"A": 1000.0, "B": 500.0}  # both long -> no short sleeve to balance against
    betas = {"A": 1.0, "B": 1.0}
    assert beta_scales(sn, betas) == {"A": 1.0, "B": 1.0}


def test_dispersion_high_when_coins_diverge():
    # one coin +10%, one -10%, one flat over the window -> non-trivial dispersion
    closes = {
        "A": _series([0.0] * 4 + [0.10]),
        "B": _series([0.0] * 4 + [-0.10]),
        "C": _series([0.0] * 5),
    }
    assert dispersion(closes, lookback=5) > 0.05


def test_dispersion_zero_when_too_few_coins():
    assert dispersion({"A": _series([0.01] * 10)}, lookback=5) == 0.0


def test_demean_by_beta_removes_beta_loading():
    # a signal that is purely a scaled copy of beta -> residual ~0 after demeaning
    betas = {"A": 0.5, "B": 1.0, "C": 1.5, "D": 2.0}
    values = {s: 3.0 * b for s, b in betas.items()}   # perfectly explained by beta
    out = demean_by_beta(values, betas)
    # residual should be ~constant (beta component removed); its correlation with beta ~0
    resid = [out[s] for s in betas]
    bs = [betas[s] for s in betas]
    rm, bm = sum(resid) / len(resid), sum(bs) / len(bs)
    cov = sum((r - rm) * (b - bm) for r, b in zip(resid, bs))
    assert abs(cov) < 1e-9


def test_demean_by_beta_preserves_orthogonal_signal():
    # a signal orthogonal to beta should be (nearly) unchanged in shape
    betas = {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0}  # no beta variance -> unchanged
    values = {"A": 1.0, "B": -1.0, "C": 2.0, "D": -2.0}
    assert demean_by_beta(values, betas) == values


def test_demean_by_beta_noop_few_names():
    assert demean_by_beta({"A": 1.0, "B": 2.0}, {"A": 1.0, "B": 2.0}) == {"A": 1.0, "B": 2.0}
