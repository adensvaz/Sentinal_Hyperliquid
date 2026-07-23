"""Hyperliquid adapter — verifies it's a faithful drop-in for the KoinbayFutures read surface
(shape-mapping) and that execution stays gated. Network is mocked so this runs in CI without keys.
"""
import pytest

from sentinel.exchange.contracts import ContractRegistry
from sentinel.exchange.hyperliquid import HL_MAKER, HL_TAKER, HyperliquidFutures

# canned Hyperliquid `info` responses
META_CTXS = [
    {"universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        {"name": "DEAD", "szDecimals": 2, "maxLeverage": 10, "isDelisted": True},
    ]},
    [
        {"funding": "0.0000125", "markPx": "60000.0", "oraclePx": "60001.0", "dayNtlVlm": "1000000000.0", "midPx": "60000.0"},
        {"funding": "-0.00005",  "markPx": "3000.0",  "oraclePx": "3000.0",  "dayNtlVlm": "500000000.0",  "midPx": "3000.0"},
        {"funding": "0.0",       "markPx": "1.0",     "oraclePx": "1.0",     "dayNtlVlm": "0",            "midPx": "1.0"},
    ],
]
CANDLES = [{"t": 1700000000000, "c": "59900.0", "v": "12.3"},
           {"t": 1700086400000, "c": "60000.0", "v": "10.0"}]


FUNDING_HIST = [{"coin": "BTC", "fundingRate": "0.0000125", "time": 1700000000000},
                {"coin": "BTC", "fundingRate": "0.0000125", "time": 1700003600000},   # same UTC day -> summed
                {"coin": "BTC", "fundingRate": "0.0000100", "time": 1700086400000}]   # next day


def _fake_post(body):
    t = body.get("type")
    if t == "metaAndAssetCtxs":
        return META_CTXS
    if t == "candleSnapshot":
        return CANDLES
    if t == "fundingHistory":
        return FUNDING_HIST
    return {}


@pytest.fixture
def fx(monkeypatch):
    f = HyperliquidFutures()
    monkeypatch.setattr(f, "_post", _fake_post)
    return f


def test_contracts_mapping(fx):
    cs = {c["symbol"]: c for c in fx.contracts()}
    assert "DEAD" not in cs                                  # delisted filtered out
    assert cs["BTC"]["multiplier"] == pytest.approx(1e-5)    # szDecimals 5 -> 10^-5
    assert cs["ETH"]["multiplier"] == pytest.approx(1e-4)
    assert cs["BTC"]["maxLever"] == 40
    assert cs["BTC"]["type"] == "E" and cs["BTC"]["status"] == 1
    assert cs["BTC"]["openTakerFee"] == HL_TAKER and cs["BTC"]["openMakerFee"] == HL_MAKER


def test_registry_is_dropin(fx):
    reg = ContractRegistry.from_futures(fx)
    assert reg.has("BTC") and reg.get("BTC").is_active_usdt
    assert len(reg.active_usdt()) == 2                       # BTC, ETH (DEAD excluded)


def test_index_funding_and_mark(fx):
    idx = fx.index("ETH")
    assert idx["currentFundRate"] == pytest.approx(-0.00005)
    assert idx["nextFundRate"] == pytest.approx(-0.00005)
    assert idx["tagPrice"] == pytest.approx(3000.0)


def test_ticker_recovers_quote_notional(fx):
    """build_universe computes vol * multiplier * last — it must recover HL's dayNtlVlm."""
    reg = ContractRegistry.from_futures(fx)
    t, s = fx.ticker("BTC"), reg.get("BTC")
    assert t["vol"] * s.multiplier * t["last"] == pytest.approx(1_000_000_000.0, rel=1e-6)


def test_klines_shape(fx):
    kl = fx.klines("BTC", "1day", 100)
    assert kl[-1] == {"idx": 1700086400000, "close": 60000.0, "open": 60000.0,
                      "high": 60000.0, "low": 60000.0, "vol": 10.0}


def test_execution_is_gated(fx):
    for m in ("create_order", "cancel_order", "positions", "account", "my_trades", "edit_leverage"):
        with pytest.raises(NotImplementedError):
            getattr(fx, m)("BTC")


def test_funding_history_daily_sum(fx):
    """The funding-harvest book measures funding CONSISTENCY from trailing daily-summed funding."""
    h = fx.funding_history("BTC", days=14)
    assert len(h) == 2
    assert h[0] == pytest.approx(0.000025)    # two same-UTC-day events summed
    assert h[1] == pytest.approx(0.00001)


def test_index_exposes_mark_and_oracle(fx):
    """The delta-neutral basis (mark vs oracle) needs both prices from index()."""
    idx = fx.index("BTC")
    assert idx["tagPrice"] == pytest.approx(60000.0)      # mark (perp)
    assert idx["indexPrice"] == pytest.approx(60001.0)    # oracle (~spot) — for the basis PnL
    assert idx["currentFundRate"] == pytest.approx(0.0000125)
