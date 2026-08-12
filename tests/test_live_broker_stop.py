"""A live position must never end up without its exchange-side stop, silently.

_rest_stop used to swallow every failure with a bare `pass`. The order would fill, the catastrophe
stop would fail to rest, and nothing — no log line, no fill status, no metric — recorded that the
position was now naked. The 60s risk monitor still enforces position_stop_pct in software, so this
was never fatal; but the exchange-side stop is precisely the protection that survives the process
dying, which is when you most need it.

These pin the two things that were missing: a retry, and visibility.
"""
import logging

import pytest

from sentinel.config import Config
from sentinel.exchange.contracts import ContractRegistry
from sentinel.execution.broker import Order
from sentinel.execution.live_broker import LiveBroker

SYM = "BTC"


def _registry():
    return ContractRegistry([{"symbol": SYM, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
                              "multiplierCoin": SYM, "pricePrecision": 2, "minOrderVolume": 1,
                              "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100,
                              "status": 1}])


class _Fx:
    """Fills orders happily; the stop is what we make fail."""
    def __init__(self, stop_fails=0):
        self.stop_fails = stop_fails          # how many stop attempts should raise
        self.stop_calls = 0
        self.orders = []

    def create_order(self, **kw):
        self.orders.append(kw)
        return "oid-1"

    def create_stop_loss(self, *a, **k):
        self.stop_calls += 1
        if self.stop_calls <= self.stop_fails:
            raise RuntimeError("exchange rejected the trigger order")
        return "stop-1"

    # unused by this path, present so the broker can construct
    def account(self, *a, **k):
        return {}


def _broker(fx):
    b = LiveBroker(fx, capital_override=10_000.0, stop_loss_pct=30.0)
    b.registry = _registry()
    return b


def _order():
    return Order(symbol=SYM, side="BUY", open="OPEN", volume=10, ref_price=60_000.0, notional=600.0)


def test_stop_failure_is_retried_then_reported(caplog):
    """One transient rejection must not leave the position unmarked — retry, then say so loudly."""
    fx = _Fx(stop_fails=99)                    # never succeeds
    b = _broker(fx)
    with caplog.at_level(logging.ERROR):
        ok = b._rest_stop(_order(), b.registry.get(SYM), Config().execution)
    assert ok is False
    assert fx.stop_calls == 2, "must retry once before giving up"
    assert any("STOP NOT RESTED" in r.message for r in caplog.records), \
        "a naked live position must produce an ERROR, never silence"


def test_transient_failure_recovers_on_the_retry():
    fx = _Fx(stop_fails=1)                     # first attempt fails, second succeeds
    b = _broker(fx)
    assert b._rest_stop(_order(), b.registry.get(SYM), Config().execution) is True
    assert fx.stop_calls == 2


def test_stop_rests_first_time_when_healthy():
    fx = _Fx(stop_fails=0)
    b = _broker(fx)
    assert b._rest_stop(_order(), b.registry.get(SYM), Config().execution) is True
    assert fx.stop_calls == 1


def test_long_and_short_triggers_sit_on_the_right_side():
    """A long stops BELOW and closes with a SELL; a short stops ABOVE and closes with a BUY.
    Getting this backwards would arm a stop that can never protect anything."""
    captured = []

    class _Cap(_Fx):
        def create_stop_loss(self, symbol, side, volume, trigger, margin):
            captured.append((side, trigger))
            return "stop"

    b = _broker(_Cap())
    spec, ex = b.registry.get(SYM), Config().execution
    b._rest_stop(Order(SYM, "BUY", "OPEN", 10, 60_000.0, 600.0), spec, ex)
    b._rest_stop(Order(SYM, "SELL", "OPEN", 10, 60_000.0, 600.0), spec, ex)
    (long_side, long_trig), (short_side, short_trig) = captured
    assert long_side == "SELL" and long_trig < 60_000.0
    assert short_side == "BUY" and short_trig > 60_000.0
