"""Grid / market-making strategy — deterministic unit tests (no network). Verifies the core mechanic
(long the dips / short the rips around the anchor), the K-level clamp, the trend filter + hard stop
recentering, the anchor persistence, and the 1x gross bound."""
import types

from sentinel.config import GridCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.grid import GridStrategy

NAMES = ["BTC", "ETH", "SOL"]


def _reg(names=NAMES):
    raw = [{"symbol": n, "type": "E", "marginCoin": "USDC", "multiplier": 0.001, "multiplierCoin": n,
            "pricePrecision": 4, "minOrderVolume": 1, "maxMarketVolume": 0, "minLever": 1,
            "maxLever": 10, "status": 1, "openTakerFee": 0.00045, "closeTakerFee": 0.00045,
            "openMakerFee": 0.00015, "closeMakerFee": 0.00015} for n in names]
    return ContractRegistry(raw)


def _strat(**kw):
    return GridStrategy(types.SimpleNamespace(grid=GridCfg(**kw)))


FLAT = [100.0] * 120          # MA == 100, so a price near 100 counts as "ranging"


def test_dip_goes_long():
    anchors = {"BTC": 100.0}
    # price 97.6 == exactly 2 spacings (1.2%) below the anchor -> LONG 2 units
    book = _strat(levels=6, spacing_pct=1.2, range_band=0.5).build_book(
        {"BTC": FLAT}, {"BTC": 97.6}, _reg(["BTC"]), 10000.0, anchors)
    p = book.positions["BTC"]
    assert p.side == "LONG" and p.target_contracts > 0
    assert round(p.score) == 2


def test_pop_goes_short():
    anchors = {"BTC": 100.0}
    # price 103.6 == exactly 3 spacings above the anchor -> SHORT 3 units
    book = _strat(levels=6, spacing_pct=1.2, range_band=0.5).build_book(
        {"BTC": FLAT}, {"BTC": 103.6}, _reg(["BTC"]), 10000.0, anchors)
    p = book.positions["BTC"]
    assert p.side == "SHORT" and p.target_contracts < 0
    assert round(p.score) == -3


def test_at_anchor_is_flat():
    anchors = {"BTC": 100.0}
    book = _strat(range_band=0.5).build_book({"BTC": FLAT}, {"BTC": 100.0}, _reg(["BTC"]), 10000.0, anchors)
    assert "BTC" not in book.positions


def test_clamped_at_K_levels():
    anchors = {"BTC": 100.0}
    # price 91 == 7.5 spacings below (within the 10% stop) -> clamps to the K=6 max
    book = _strat(levels=6, spacing_pct=1.2, stop_pct=10.0, range_band=0.5).build_book(
        {"BTC": FLAT}, {"BTC": 91.0}, _reg(["BTC"]), 10000.0, anchors)
    p = book.positions["BTC"]
    assert p.side == "LONG" and round(p.score) == 6


def test_trend_filter_stands_aside_and_recenters():
    anchors = {"BTC": 150.0}
    closes = {"BTC": [100.0 + i for i in range(120)]}   # steadily rising -> price far above its MA
    price = closes["BTC"][-1]
    book = _strat(range_band=0.08, ma_period=100).build_book(
        closes, {"BTC": price}, _reg(["BTC"]), 10000.0, anchors)
    assert "BTC" not in book.positions          # not ranging -> stand aside
    assert anchors["BTC"] == price               # recentered to the current price


def test_hard_stop_recenters():
    anchors = {"BTC": 100.0}
    # price 88 == 12% below the anchor, past the 10% stop -> flatten + recenter (tail cap)
    book = _strat(stop_pct=10.0, range_band=0.5).build_book(
        {"BTC": FLAT}, {"BTC": 88.0}, _reg(["BTC"]), 10000.0, anchors)
    assert "BTC" not in book.positions
    assert anchors["BTC"] == 88.0


def test_first_sight_sets_anchor_and_stays_flat():
    anchors = {}                                  # never seen before
    book = _strat().build_book({"BTC": FLAT}, {"BTC": 100.0}, _reg(["BTC"]), 10000.0, anchors)
    assert "BTC" not in book.positions
    assert anchors["BTC"] == 100.0                # anchor initialized here


def test_gross_bounded_by_target_at_full_extension():
    # all three coins fully extended (6 levels down, within the stop) -> total gross must not exceed 1x equity
    anchors = {n: 100.0 for n in NAMES}
    prices = {n: 92.8 for n in NAMES}             # 92.8 == 6 spacings below anchor
    book = _strat(levels=6, spacing_pct=1.2, stop_pct=10.0, range_band=0.5, target_gross=1.0).build_book(
        {n: FLAT for n in NAMES}, prices, _reg(), 10000.0, anchors)
    assert len(book.positions) == 3
    assert all(p.side == "LONG" for p in book.positions.values())
    assert book.realized_gross <= 10000.0 * 1.02  # ~1x, never levered up
