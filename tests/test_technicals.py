from sentinel.signal.technicals import passes_veto, ta_consensus, ta_label


def test_uptrend_is_bullish():
    c = [100 * (1.01 ** i) for i in range(60)]
    assert ta_consensus(c) > 0.3


def test_downtrend_is_bearish():
    c = [100 * (0.99 ** i) for i in range(60)]
    assert ta_consensus(c) < -0.3


def test_insufficient_data_is_neutral():
    assert ta_consensus([100, 101, 102]) == 0.0


def test_labels():
    assert ta_label(0.6) == "Strong Buy"
    assert ta_label(0.2) == "Buy"
    assert ta_label(0.0) == "Neutral"
    assert ta_label(-0.2) == "Sell"
    assert ta_label(-0.6) == "Strong Sell"


def test_veto_rules():
    assert passes_veto(-0.5, "LONG", 0.3) is False     # bearish -> no long
    assert passes_veto(0.0, "LONG", 0.3) is True
    assert passes_veto(0.5, "SHORT", 0.3) is False     # bullish -> no short
    assert passes_veto(-0.5, "SHORT", 0.3) is True
    assert passes_veto(-0.9, "LONG", 0.0) is True       # threshold 0 disables the veto
