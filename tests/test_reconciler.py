from sentinel.exchange.contracts import ContractRegistry
from sentinel.execution.reconciler import reconcile

# multiplier 1.0, price 1.0 -> notional == contract count, so min_notional is in "contracts"
REG = ContractRegistry([{
    "symbol": "E-X-USDT", "type": "E", "marginCoin": "USDT", "multiplier": 1.0,
    "multiplierCoin": "X", "pricePrecision": 2, "minOrderVolume": 1,
    "maxMarketVolume": 10_000_000, "minLever": 0, "maxLever": 100, "status": 1,
}])
PRICES = {"E-X-USDT": 1.0}


def _orders(cur, tgt, min_notional=5.0):
    return reconcile({"E-X-USDT": tgt} if tgt else {},
                     {"E-X-USDT": cur} if cur else {},
                     PRICES, REG, min_notional)


def test_open_new_long():
    o = _orders(0, 100)
    assert len(o) == 1 and o[0].side == "BUY" and o[0].open == "OPEN" and o[0].volume == 100


def test_open_new_short():
    o = _orders(0, -100)
    assert len(o) == 1 and o[0].side == "SELL" and o[0].open == "OPEN" and o[0].volume == 100


def test_grow_long_opens_delta():
    o = _orders(100, 150)
    assert len(o) == 1 and o[0].side == "BUY" and o[0].open == "OPEN" and o[0].volume == 50


def test_shrink_long_closes_delta():
    o = _orders(100, 60)
    assert len(o) == 1 and o[0].side == "SELL" and o[0].open == "CLOSE" and o[0].volume == 40


def test_full_close_long():
    o = _orders(100, 0)
    assert len(o) == 1 and o[0].side == "SELL" and o[0].open == "CLOSE" and o[0].volume == 100


def test_flip_long_to_short_is_close_then_open():
    o = _orders(100, -80)
    assert len(o) == 2
    assert (o[0].side, o[0].open, o[0].volume) == ("SELL", "CLOSE", 100)
    assert (o[1].side, o[1].open, o[1].volume) == ("SELL", "OPEN", 80)


def test_tiny_adjustment_skipped():
    assert _orders(100, 103, min_notional=5) == []   # delta 3 < 5


def test_dust_open_skipped():
    assert _orders(0, 3, min_notional=5) == []


def test_full_close_ignores_min_notional():
    # exiting a dust position must still execute even though notional < min
    o = _orders(3, 0, min_notional=5)
    assert len(o) == 1 and o[0].open == "CLOSE" and o[0].volume == 3


def test_rebalance_band_skips_small_same_side_drift():
    # 100 -> 105 is a 5% drift; band is 10% -> no trade
    assert reconcile({"E-X-USDT": 105}, {"E-X-USDT": 100}, PRICES, REG, 0.0, 0.10) == []
    # 100 -> 120 is 20% > 10% -> trade the 20-contract delta
    o = reconcile({"E-X-USDT": 120}, {"E-X-USDT": 100}, PRICES, REG, 0.0, 0.10)
    assert len(o) == 1 and o[0].open == "OPEN" and o[0].volume == 20


def test_rebalance_band_does_not_block_new_or_close():
    # band only applies to same-side adjustments; opening new and full close still fire
    assert len(reconcile({"E-X-USDT": 5}, {}, PRICES, REG, 0.0, 0.10)) == 1          # new open
    assert len(reconcile({}, {"E-X-USDT": 100}, PRICES, REG, 0.0, 0.10)) == 1        # full close
