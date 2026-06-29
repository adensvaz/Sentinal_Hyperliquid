"""Tests for the champion (momentum + regime) strategy and its two validated improvements:
breadth-scaled gross and capped momentum-weighted sizing."""
from sentinel.config import Config, ChampionCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.momentum_regime import (
    MomentumRegimeStrategy, breadth_fraction, breadth_scale, capped_weights,
)

N = 140  # bars of daily history (enough for 100d MAs)


def _registry(symbols):
    raw = [{"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
            "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
            "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1}
           for s in symbols]
    return ContractRegistry(raw)


def _series(start, g):
    """Monotonic geometric series of length N. g>0 rising (strong/above-MA/+momentum), g<0 falling."""
    return [start * (1.0 + g) ** k for k in range(N)]


def _cfg(**champ):
    base = dict(top_k=5, lookback=30, regime_ma=100, dual_confirm=False, target_gross=1.0,
                breadth_scale=False, breadth_ma=100, mom_weighted=False)
    base.update(champ)
    return Config(strategy="champion", champion=ChampionCfg(**base))


# ---------- helper invariants ----------
def test_breadth_scale_ramp():
    assert breadth_scale(0.2, 0.3, 0.8) == 0.0     # below floor -> flat
    assert breadth_scale(0.9, 0.3, 0.8) == 1.0     # above ceiling -> full
    assert abs(breadth_scale(0.55, 0.3, 0.8) - 0.5) < 1e-9  # midpoint -> half


def test_capped_weights_respects_cap_and_sums_to_one():
    w = capped_weights({"A": 0.9, "B": 0.05, "C": 0.03, "D": 0.02}, 0.40)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert max(w.values()) <= 0.40 + 1e-9
    assert w["A"] == max(w.values())  # strongest still the largest, just clamped


def test_breadth_fraction_counts_uptrends():
    up, down = _series(100, 0.01), _series(100, -0.01)
    assert breadth_fraction([up, up, up, down], 100) == 0.75


# ---------- regime brake ----------
def test_regime_off_returns_cash():
    syms = ["E-BTC-USDT"] + [f"E-C{i}-USDT" for i in range(4)]
    reg = _registry(syms)
    prices = {s: 100.0 for s in syms}
    closes = {s: _series(100, 0.01) for s in syms}
    closes["E-BTC-USDT"] = _series(200, -0.01)  # BTC falling -> below its 100d MA -> risk-off
    book = MomentumRegimeStrategy(_cfg()).build_book(closes, prices, reg, equity=10_000)
    assert book.positions == {}


# ---------- selection + equal weight ----------
def test_regime_on_equal_weight_picks_topk():
    syms = ["E-BTC-USDT"] + [f"E-C{i}-USDT" for i in range(6)]
    reg = _registry(syms)
    prices = {s: 100.0 for s in syms}
    closes = {"E-BTC-USDT": _series(100, 0.0005)}               # BTC barely rising -> risk-on, weak momentum
    for i in range(6):
        closes[f"E-C{i}-USDT"] = _series(100, 0.002 * (i + 1))   # C5 strongest ... C0 weakest, all > BTC
    book = MomentumRegimeStrategy(_cfg(top_k=3)).build_book(closes, prices, reg, equity=10_000)
    held = set(book.positions)
    assert held == {"E-C5-USDT", "E-C4-USDT", "E-C3-USDT"}       # the 3 strongest (BTC outranked)
    notion = [p.target_notional for p in book.positions.values()]
    assert max(notion) - min(notion) < max(notion) * 0.05        # ~equal weight


# ---------- breadth scaling cuts gross ----------
def test_breadth_scaling_reduces_gross_when_market_narrow():
    syms = ["E-BTC-USDT"] + [f"E-C{i}-USDT" for i in range(7)]
    reg = _registry(syms)
    prices = {s: 100.0 for s in syms}
    cfg = _cfg(top_k=3, breadth_scale=True, breadth_lo=0.0, breadth_hi=1.0)

    # broad market: everything rising -> breadth ~1.0 -> full gross
    broad = {s: _series(100, 0.01) for s in syms}
    g_broad = MomentumRegimeStrategy(cfg).build_book(broad, prices, reg, 10_000).realized_gross

    # narrow market: only the 3 leaders + BTC rising, the other 4 falling -> breadth ~0.5 -> half gross
    narrow = {"E-BTC-USDT": _series(100, 0.01)}
    for i in range(7):
        narrow[f"E-C{i}-USDT"] = _series(100, 0.01 if i < 3 else -0.01)
    g_narrow = MomentumRegimeStrategy(cfg).build_book(narrow, prices, reg, 10_000).realized_gross

    assert g_narrow < g_broad * 0.7      # clearly de-risked
    assert g_narrow > 0                  # but still invested


# ---------- momentum-weighted sizing respects the per-name cap ----------
def test_momentum_weighted_caps_single_name():
    syms = ["E-BTC-USDT"] + [f"E-C{i}-USDT" for i in range(5)]
    reg = _registry(syms)
    prices = {s: 100.0 for s in syms}
    closes = {"E-BTC-USDT": _series(100, 0.005)}
    closes["E-C0-USDT"] = _series(100, 0.05)                     # one runaway leader
    for i in range(1, 5):
        closes[f"E-C{i}-USDT"] = _series(100, 0.002)             # modest names
    book = MomentumRegimeStrategy(
        _cfg(top_k=5, mom_weighted=True, max_name_weight=0.40)
    ).build_book(closes, prices, reg, equity=10_000)
    gross = book.realized_gross
    top = max(p.target_notional for p in book.positions.values())
    assert top <= gross * 0.40 + gross * 0.02                    # capped near 40% (+rounding slack)
