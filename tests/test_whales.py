from sentinel.signal.base import SymbolData
from sentinel.signal.market_proxy import MarketProxySignal
from sentinel.signal.whale_select import rank_candidates
from sentinel.signal.whale_tracker import WhaleSnap, WhaleTracker, map_bias_to_symbols


def _row(addr, pnl, roi, wk, win, days):
    return {"address": addr, "month_pnl": pnl, "month_roi": roi, "week_pnl": wk,
            "win_rate": win, "days_since": days}


def test_rank_prefers_money_winrate_recency():
    rows = [
        _row("best", 1_000_000, 1.0, 200_000, 0.90, 0.5),
        _row("mid", 500_000, 0.5, 100_000, 0.70, 2.0),
        _row("stale", 800_000, 0.8, 10_000, 0.65, 4.9),
    ]
    out = rank_candidates(rows, top=3, min_win_rate=0.6, max_inactive_days=5)
    assert out[0]["address"] == "best"   # dominates money + roi + winrate + recency


def test_rank_winrate_gate_excludes_low_when_enough_qualify():
    rows = [
        _row("a", 1_000_000, 1.0, 100_000, 0.80, 1),
        _row("b", 900_000, 0.9, 100_000, 0.75, 1),
        _row("low", 1_200_000, 1.2, 200_000, 0.40, 0.2),  # most money but 40% win < gate
    ]
    out = rank_candidates(rows, top=2, min_win_rate=0.6, max_inactive_days=5)
    assert {r["address"] for r in out} == {"a", "b"}   # 'low' filtered by the win-rate gate

H4 = 4 * 3_600_000


def _tracker(weighting="equal"):
    return WhaleTracker(addresses=[], weighting=weighting)  # no network used by blended_bias


def test_blended_bias_normalizes_per_wallet():
    snaps = [
        WhaleSnap("0xA", equity=100, gross=100, positions={"BTC": 100}),          # +1.0 BTC
        WhaleSnap("0xB", equity=200, gross=200, positions={"BTC": -100, "ETH": 100}),  # -0.5 BTC, +0.5 ETH
    ]
    bias = _tracker().blended_bias(snaps)
    assert round(bias["BTC"], 4) == 0.25   # (1.0 + -0.5)/2
    assert round(bias["ETH"], 4) == 0.25   # (0 + 0.5)/2


def test_mega_whale_does_not_dominate_under_equal_weight():
    snaps = [
        WhaleSnap("0xsmall", equity=100, gross=100, positions={"BTC": 100}),        # +1.0
        WhaleSnap("0xmega", equity=1e8, gross=10000, positions={"BTC": -10000}),    # -1.0
    ]
    bias = _tracker("equal").blended_bias(snaps)
    assert abs(bias["BTC"]) < 1e-9   # cancels despite the mega-whale's $ size


def test_equity_weighting_lets_bigger_account_win():
    snaps = [
        WhaleSnap("0xsmall", equity=100, gross=100, positions={"BTC": 100}),     # +1.0, weight 100
        WhaleSnap("0xbig", equity=900, gross=100, positions={"BTC": -100}),      # -1.0, weight 900
    ]
    bias = _tracker("equity").blended_bias(snaps)
    assert bias["BTC"] < 0   # the larger account tilts it short


def test_map_bias_to_symbols_filters_unknown():
    out = map_bias_to_symbols({"BTC": 0.5, "ETH": -0.3, "FOO": 1.0},
                              {"BTC": "E-BTC-USDT", "ETH": "E-ETH-USDT"})
    assert out == {"E-BTC-USDT": 0.5, "E-ETH-USDT": -0.3}   # FOO dropped (not on KoinBay)


def test_whale_component_shifts_score():
    flat = ([1_700_000_000_000 + i * H4 for i in range(10)], [100.0] * 10, [1.0] * 10)
    data = {"E-A-USDT": SymbolData("E-A-USDT", *flat), "E-B-USDT": SymbolData("E-B-USDT", *flat)}
    sig = MarketProxySignal(horizons_hours=[4],
                            weights={"momentum": 0, "funding": 0, "volume": 0, "whale": 1.0})
    scores = sig.scores(data, whale_bias={"E-A-USDT": 1.0, "E-B-USDT": -1.0})
    assert scores["E-A-USDT"] > scores["E-B-USDT"]   # whales long A, short B
