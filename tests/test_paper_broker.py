from types import SimpleNamespace

from sentinel.exchange.contracts import ContractRegistry
from sentinel.execution.broker import Order
from sentinel.execution.paper_broker import PaperBroker


def _registry(fee=0.0, multiplier=1.0):
    return ContractRegistry([{
        "symbol": "E-X-USDT", "type": "E", "marginCoin": "USDT", "multiplier": multiplier,
        "multiplierCoin": "X", "pricePrecision": 2, "minOrderVolume": 1,
        "maxMarketVolume": 10_000_000, "minLever": 0, "maxLever": 100, "status": 1,
        "openTakerFee": fee, "closeTakerFee": fee,
    }])


EXEC = SimpleNamespace(order_type="limit", slippage_bps=0.0)


def _order(side, open_, vol, price):
    return Order("E-X-USDT", side, open_, vol, price, vol * price)


def test_long_unrealized_then_realized_pnl():
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("BUY", "OPEN", 100, 100.0)], reg, EXEC)
    assert b.positions() == {"E-X-USDT": 100}
    b.set_marks({"E-X-USDT": 110.0})
    assert b.equity() == 11_000.0           # +10/contract * 100 unrealized
    b.execute([_order("SELL", "CLOSE", 100, 110.0)], reg, EXEC)
    assert b.positions() == {}
    assert b.equity() == 11_000.0           # locked in as realized
    assert b.realized_pnl == 1_000.0


def test_short_profits_when_price_falls():
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("SELL", "OPEN", 100, 100.0)], reg, EXEC)
    assert b.positions() == {"E-X-USDT": -100}
    b.set_marks({"E-X-USDT": 90.0})
    assert b.equity() == 11_000.0           # short gains 10/contract


def test_fees_reduce_equity():
    reg = _registry(fee=0.001)
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("BUY", "OPEN", 100, 100.0)], reg, EXEC)   # notional 10k * 0.001 = 10 fee
    b.set_marks({"E-X-USDT": 100.0})
    assert round(b.equity(), 6) == 9_990.0
    assert round(b.fees_paid, 6) == 10.0


def test_closed_trade_recorded_on_close():
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("BUY", "OPEN", 100, 100.0)], reg, EXEC)
    b.execute([_order("SELL", "CLOSE", 100, 110.0)], reg, EXEC)
    assert len(b.closed_trades) == 1
    t = b.closed_trades[0]
    assert t["side"] == "LONG" and t["contracts"] == 100
    assert round(t["pnl"], 2) == 1000.0 and t["entry"] == 100.0 and t["exit"] == 110.0


def test_funding_short_collects_positive_rate():
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("SELL", "OPEN", 100, 100.0)], reg, EXEC)   # short, notional 10k
    b.accrue_funding({"E-X-USDT": 0.0001}, {"E-X-USDT": 100.0}, interval_hours=8, now=1000.0)  # sets clock
    assert b.funding_pnl == 0.0
    b.accrue_funding({"E-X-USDT": 0.0001}, {"E-X-USDT": 100.0}, interval_hours=8, now=1000.0 + 8 * 3600)
    assert round(b.funding_pnl, 6) == 1.0    # short collects rate*notional over one interval
    b.set_marks({"E-X-USDT": 100.0})
    assert round(b.equity() - 10_000, 6) == 1.0


def test_funding_long_pays_positive_rate():
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("BUY", "OPEN", 100, 100.0)], reg, EXEC)
    b.accrue_funding({"E-X-USDT": 0.0001}, {"E-X-USDT": 100.0}, 8, 1000.0)
    b.accrue_funding({"E-X-USDT": 0.0001}, {"E-X-USDT": 100.0}, 8, 1000.0 + 8 * 3600)
    assert round(b.funding_pnl, 6) == -1.0   # long pays when funding is positive


def test_funding_hourly_interval_accrues_8x_of_8h():
    """Fix #1: Hyperliquid funding is HOURLY. With interval_hours=1 the same rate over the same
    8h window must accrue 8x what interval_hours=8 (KoinBay) does — else the funding edge is
    under-credited 8x. Mirrors test_funding_short_collects_positive_rate (which uses 8h -> 1.0)."""
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("SELL", "OPEN", 100, 100.0)], reg, EXEC)          # short, notional 10k
    b.accrue_funding({"E-X-USDT": 0.0001}, {"E-X-USDT": 100.0}, interval_hours=1, now=1000.0)
    b.accrue_funding({"E-X-USDT": 0.0001}, {"E-X-USDT": 100.0}, interval_hours=1, now=1000.0 + 8 * 3600)
    assert round(b.funding_pnl, 6) == 8.0    # 8 hourly intervals * rate*notional (8x the 8h figure of 1.0)


def test_venue_funding_intervals_are_correct():
    """Fix #1 wiring: each adapter declares its funding cadence so the engine passes the right interval."""
    from sentinel.exchange.futures import KoinbayFutures
    from sentinel.exchange.hyperliquid import HyperliquidFutures
    assert HyperliquidFutures.funding_interval_hours == 1.0     # hourly
    assert KoinbayFutures.funding_interval_hours == 8.0         # 8-hourly


def _registry_mt(maker_fee, taker_fee, multiplier=1.0):
    return ContractRegistry([{
        "symbol": "E-X-USDT", "type": "E", "marginCoin": "USDT", "multiplier": multiplier,
        "multiplierCoin": "X", "pricePrecision": 2, "minOrderVolume": 1,
        "maxMarketVolume": 10_000_000, "minLever": 0, "maxLever": 100, "status": 1,
        "openMakerFee": maker_fee, "closeMakerFee": maker_fee,
        "openTakerFee": taker_fee, "closeTakerFee": taker_fee,
    }])


def test_maker_fill_ratio_blends_maker_and_taker():
    """Fix #2: maker_fill_ratio<1 means part of the order chases as taker — worse price AND fee
    than the optimistic all-maker assumption, so paper isn't rose-tinted."""
    reg = _registry_mt(maker_fee=0.0001, taker_fee=0.0005)
    # optimistic: all maker, fill at touch, maker fee only
    b_full = PaperBroker(starting_capital=10_000)
    b_full.execute([_order("BUY", "OPEN", 100, 100.0)], reg,
                   SimpleNamespace(order_type="limit", slippage_bps=10.0, maker=True, maker_fill_ratio=1.0))
    assert round(b_full._positions["E-X-USDT"].avg_price, 4) == 100.00
    assert round(b_full.fees_paid, 6) == 1.0                    # 100 * 100 * 0.0001

    # realistic: half maker / half taker -> blended fill 100.05, blended fee_rate 0.0003
    b_half = PaperBroker(starting_capital=10_000)
    b_half.execute([_order("BUY", "OPEN", 100, 100.0)], reg,
                   SimpleNamespace(order_type="limit", slippage_bps=10.0, maker=True, maker_fill_ratio=0.5))
    assert round(b_half._positions["E-X-USDT"].avg_price, 4) == 100.05   # 0.5*100.00 + 0.5*100.10
    assert round(b_half.fees_paid, 4) == round(100 * 100.05 * 0.0003, 4)  # 0.5*maker + 0.5*taker
    assert b_half.fees_paid > b_full.fees_paid                  # realism costs more than the optimistic path


def test_flip_realizes_and_reverses():
    reg = _registry()
    b = PaperBroker(starting_capital=10_000)
    b.execute([_order("BUY", "OPEN", 100, 100.0)], reg, EXEC)
    # flip to short 80: close 100 @120 (realize +2000), open 80 short @120
    b.execute([_order("SELL", "CLOSE", 100, 120.0), _order("SELL", "OPEN", 80, 120.0)], reg, EXEC)
    assert b.positions() == {"E-X-USDT": -80}
    assert b.realized_pnl == 2_000.0
    b.set_marks({"E-X-USDT": 120.0})
    assert b.equity() == 12_000.0
