"""The book probe is what makes paper training legitimate, so it has to be right about cost.

If it under-prices crossing, the policy will cross too much live. If it silently prices an order
the book cannot absorb, every number downstream is fiction. These tests pin both.
"""
import pytest

from sentinel.execution.book_probe import BookState, post_outcome_bps, probe


class _Fx:
    """Book with a controllable ladder. sz is in contracts; notional at a level = px * sz."""
    def __init__(self, bids, asks):
        self._b, self._a = bids, asks

    def depth(self, symbol):
        return {"levels": [[{"px": str(p), "sz": str(s)} for p, s in self._b],
                           [{"px": str(p), "sz": str(s)} for p, s in self._a]]}


def _deep():
    return _Fx(bids=[(99.99, 1000), (99.98, 1000)], asks=[(100.01, 1000), (100.02, 1000)])


def test_reads_spread_and_mid():
    bs = probe(_deep(), "X", "BUY", 100.0)
    assert bs.mid == pytest.approx(100.0, abs=1e-6)
    assert bs.spread_bps == pytest.approx(2.0, abs=0.05)


def test_small_order_costs_about_half_the_spread():
    """Crossing for size that fits on the touch should cost ~half-spread, not a full spread."""
    bs = probe(_deep(), "X", "BUY", 500.0)
    assert bs.cross_bps == pytest.approx(1.0, abs=0.2)


def test_large_order_walks_the_book_and_costs_more():
    """THE test. A size that eats several levels must price worse than one that does not —
    this is the whole reason a flat slippage_bps is wrong."""
    thin = _Fx(bids=[(99.9, 1)], asks=[(100.1, 1), (101.0, 1), (105.0, 100)])
    small = probe(thin, "X", "BUY", 50.0).cross_bps
    large = probe(thin, "X", "BUY", 400.0).cross_bps
    assert large > small * 2, f"walking the book was not priced: {small:.2f} -> {large:.2f}"


def test_unabsorbable_order_is_not_priced_at_the_touch():
    """If the visible book cannot fill us, the remainder must be priced at the worst level, not
    silently assumed to fill at the front. This is where optimistic backtests are born."""
    tiny = _Fx(bids=[(99.0, 1)], asks=[(100.0, 1), (200.0, 1)])
    bs = probe(tiny, "X", "BUY", 100_000.0)
    assert bs.cross_bps > 1000.0, f"absorbed an impossible order at {bs.cross_bps:.1f}bps"
    assert bs.thin


def test_sell_side_uses_bids_and_is_signed_correctly():
    bs = probe(_deep(), "X", "SELL", 500.0)
    assert bs.cross_bps > 0, "selling into the bid must also be a positive cost"


def test_thin_flag_tracks_depth_against_our_size():
    assert probe(_deep(), "X", "BUY", 10.0).thin is False
    assert probe(_deep(), "X", "BUY", 90_000.0).thin is True


def test_bad_books_return_none_rather_than_guessing():
    class Empty:
        def depth(self, s):
            return {"levels": []}

    class Crossed:
        def depth(self, s):
            return {"levels": [[{"px": "101", "sz": "1"}], [{"px": "99", "sz": "1"}]]}

    class Boom:
        def depth(self, s):
            raise RuntimeError("network")

    assert probe(Empty(), "X", "BUY", 100.0) is None
    assert probe(Crossed(), "X", "BUY", 100.0) is None
    assert probe(Boom(), "X", "BUY", 100.0) is None


# ------------------------------------------------------------------ passive outcomes
def test_resting_buy_that_traded_through_earns_the_spread():
    cost, filled = post_outcome_bps("BUY", touch_price=99.99, arrival_mid=100.0,
                                    low=99.90, high=100.20, completion_bps=8.0)
    assert filled and cost < 0, "a filled passive buy at the bid should be a negative cost"


def test_resting_buy_that_never_traded_is_charged_the_chase():
    """The load-bearing rule: a miss costs what it took to finish, never zero."""
    cost, filled = post_outcome_bps("BUY", touch_price=99.99, arrival_mid=100.0,
                                    low=100.05, high=100.40, completion_bps=8.0)
    assert not filled
    assert cost == pytest.approx(8.0), "an unfilled passive order must carry its completion cost"


def test_resting_sell_symmetry():
    cost, filled = post_outcome_bps("SELL", touch_price=100.01, arrival_mid=100.0,
                                    low=99.80, high=100.10, completion_bps=8.0)
    assert filled and cost < 0
    cost2, filled2 = post_outcome_bps("SELL", touch_price=100.01, arrival_mid=100.0,
                                      low=99.50, high=99.99, completion_bps=8.0)
    assert not filled2 and cost2 == pytest.approx(8.0)


def test_degenerate_inputs_are_safe():
    assert post_outcome_bps("BUY", 0.0, 100.0, 1.0, 2.0, 5.0) == (0.0, False)
    assert post_outcome_bps("BUY", 100.0, 0.0, 1.0, 2.0, 5.0) == (0.0, False)
