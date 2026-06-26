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
