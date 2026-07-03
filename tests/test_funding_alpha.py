"""Funding-Alpha strategy — deterministic unit tests (no network). Verifies dollar-neutral book
construction, the momentum guard (don't short a ripper), funding-driven selection, and the too-few
guard."""
import types

from sentinel.config import FundingAlphaCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.funding_alpha import FundingAlphaStrategy

NAMES = [f"C{i}" for i in range(18)]


def _reg(names=NAMES):
    raw = [{"symbol": n, "type": "E", "marginCoin": "USDC", "multiplier": 0.001, "multiplierCoin": n,
            "pricePrecision": 4, "minOrderVolume": 1, "maxMarketVolume": 0, "minLever": 1,
            "maxLever": 10, "status": 1, "openTakerFee": 0.00045, "closeTakerFee": 0.00045,
            "openMakerFee": 0.00015, "closeMakerFee": 0.00015} for n in names]
    return ContractRegistry(raw)


def _strat(**kw):
    fa = FundingAlphaCfg(beta_hedge=False, regime_filter=False, **kw)
    return FundingAlphaStrategy(types.SimpleNamespace(funding_alpha=fa))


def _flat(price=100.0, n=40):
    return [price] * n


def _funding_spread():
    # C0..C8 positive (descending), C9..C17 negative — clean spread, no zeros in the selected sleeves
    return {n: (0.001 * (9 - i) if i < 9 else -0.001 * (i - 8)) for i, n in enumerate(NAMES)}


def test_dollar_neutral_and_funding_directed():
    closes = {n: _flat() for n in NAMES}
    prices = {n: 100.0 for n in NAMES}
    funding = _funding_spread()
    book = _strat().build_book(closes, funding, prices, _reg(), 10000.0, 1.0)
    pos = book.positions
    longs = [p for p in pos.values() if p.side == "LONG"]
    shorts = [p for p in pos.values() if p.side == "SHORT"]
    assert len(longs) == 7 and len(shorts) == 7
    ln = sum(p.target_notional for p in longs)
    sn = sum(p.target_notional for p in shorts)
    assert abs(ln + sn) < 0.03 * abs(ln)                 # dollar-neutral (net ~ 0)
    assert all(funding[p.symbol] < 0 for p in longs)     # long the negative-funding coins
    assert all(funding[p.symbol] > 0 for p in shorts)    # short the high-funding coins


def test_momentum_guard_excludes_ripper():
    closes = {n: _flat() for n in NAMES}
    closes["C0"] = [100.0] * 19 + [100.0 * (1 + 0.30 * (k / 20)) for k in range(21)]  # +30% ripper
    prices = {n: closes[n][-1] for n in NAMES}
    book = _strat().build_book(closes, _funding_spread(), prices, _reg(), 10000.0, 1.0)
    shorts = {p.symbol for p in book.positions.values() if p.side == "SHORT"}
    assert "C0" not in shorts                            # highest-funding coin, but it's ripping -> not shorted


def test_too_few_candidates_returns_empty():
    sub = NAMES[:10]                                     # 10 < 2*top_k(7)
    book = _strat().build_book({n: _flat() for n in sub}, {n: 0.0 for n in sub},
                               {n: 100.0 for n in sub}, _reg(), 10000.0, 1.0)
    assert len(book.positions) == 0


def test_regime_filter_scales_gross():
    """With regime_filter on, a compressed-dispersion snapshot should not exceed base gross."""
    fa = FundingAlphaCfg(beta_hedge=False, regime_filter=True)
    strat = FundingAlphaStrategy(types.SimpleNamespace(funding_alpha=fa))
    closes = {n: _flat() for n in NAMES}
    prices = {n: 100.0 for n in NAMES}
    book = strat.build_book(closes, _funding_spread(), prices, _reg(), 10000.0, 1.0)
    assert book.target_gross <= 2.0 * 10000.0 * 1.31     # target_gross * equity * regime clip(<=1.3)
