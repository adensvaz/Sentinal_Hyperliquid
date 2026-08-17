"""The learned policy must be able to observe live orders without being able to break them.

Shadow mode is the whole safety argument: the policy watches every order, records what it would
have done, and has no authority until store.exec_stats() proves it beats the fixed default. These
tests assert that separation actually holds in code, because "it only logs" is exactly the kind of
claim that quietly stops being true.
"""
import tempfile

from sentinel.execution.broker import Order
from sentinel.execution.exec_policy import ARMS, ExecutionPolicy
from sentinel.execution.live_broker import LiveBroker
from sentinel.exchange.contracts import ContractRegistry
from sentinel.state.store import Store

SYM = "BTC"


def _registry():
    return ContractRegistry([{"symbol": SYM, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
                              "multiplierCoin": SYM, "pricePrecision": 2, "minOrderVolume": 1,
                              "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100,
                              "status": 1}])


class _Cfg:
    maker = True
    order_type = "limit"
    slippage_bps = 5
    time_in_force = "IOC"
    margin_model_code = 1


class _Fx:
    """Records every order the broker actually sends."""
    def __init__(self):
        self.sent = []

    def create_order(self, **kw):
        self.sent.append(kw)
        return f"oid{len(self.sent)}"


def _order():
    return Order(symbol=SYM, side="BUY", open="OPEN", volume=10, ref_price=100.0, notional=1000.0)


def _store():
    return Store(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)


def test_shadow_mode_does_not_change_what_is_sent():
    """THE safety test. With policy_live=False the wire behaviour must be byte-identical to
    having no policy at all, whatever the policy happens to think."""
    fx_a, fx_b = _Fx(), _Fx()
    plain = LiveBroker(fx_a)
    shadow = LiveBroker(fx_b, exec_policy=ExecutionPolicy(seed=1), store=_store())
    reg, cfg = _registry(), _Cfg()
    plain.execute([_order()], reg, cfg)
    shadow.execute([_order()], reg, cfg)
    assert fx_a.sent == fx_b.sent, "shadow policy altered a live order"
    assert fx_b.sent[0]["time_in_force"] == "POST_ONLY"


def test_shadow_mode_still_records_what_it_would_have_done():
    st = _store()
    b = LiveBroker(_Fx(), exec_policy=ExecutionPolicy(seed=1), store=st)
    b.execute([_order()], _registry(), _Cfg())
    rows = st.unresolved_exec("live")
    assert len(rows) == 1
    assert rows[0]["shadow"] in ARMS
    assert rows[0]["arrival_mid"] == 100.0
    assert rows[0]["cost_bps"] is None, "outcome must stay unknown until it is actually observed"


def test_policy_failure_never_blocks_an_order():
    """An execution learner that can halt trading is a liability, not an asset."""
    class Broken:
        def context(self, **kw):
            raise RuntimeError("boom")

        def choose(self, *a, **k):
            raise RuntimeError("boom")

    fx = _Fx()
    b = LiveBroker(fx, exec_policy=Broken(), store=_store())
    fills = b.execute([_order()], _registry(), _Cfg())
    assert len(fx.sent) == 1 and len(fills) == 1


def test_broken_store_never_blocks_an_order():
    class BadStore:
        def record_exec(self, *a, **k):
            raise RuntimeError("disk full")

    fx = _Fx()
    b = LiveBroker(fx, exec_policy=ExecutionPolicy(seed=1), store=BadStore())
    b.execute([_order()], _registry(), _Cfg())
    assert len(fx.sent) == 1


def test_policy_live_can_override_but_only_when_enabled():
    """When it HAS earned authority, choosing CROSS must actually stop the POST_ONLY."""
    class AlwaysCross(ExecutionPolicy):
        def choose(self, *a, **k):
            return "CROSS"

    fx = _Fx()
    b = LiveBroker(fx, exec_policy=AlwaysCross(seed=1), store=_store(), policy_live=True)
    b.execute([_order()], _registry(), _Cfg())
    assert fx.sent[0]["time_in_force"] != "POST_ONLY", "policy_live had no effect"


def test_exec_stats_scores_shadow_against_reality():
    st = _store()
    rid = st.record_exec("live", symbol=SYM, side="BUY", context="us_open|s0|v0|N",
                         arm="POST", shadow="CROSS", arrival_mid=100.0, notional=1000.0)
    st.resolve_exec(rid, fill_price=100.05, cost_bps=5.0)
    s = st.exec_stats("live")
    assert s["n"] == 1
    assert s["by_arm"]["POST"]["mean_bps"] == 5.0
    assert s["agreement"] == 0.0          # shadow disagreed on this order


def test_exec_stats_empty_is_safe():
    assert _store().exec_stats("live")["n"] == 0
