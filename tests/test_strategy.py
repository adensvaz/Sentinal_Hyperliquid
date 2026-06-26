from sentinel.config import PortfolioCfg
from sentinel.exchange.contracts import ContractRegistry
from sentinel.strategy.sentiment_edge import SentimentEdgeStrategy


def _registry(symbols):
    raw = []
    for s in symbols:
        raw.append({"symbol": s, "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
                    "multiplierCoin": s, "pricePrecision": 2, "minOrderVolume": 1,
                    "maxMarketVolume": 100_000_000, "minLever": 0, "maxLever": 100, "status": 1})
    return ContractRegistry(raw)


def test_long_short_selection_and_dollar_neutrality():
    syms = [f"E-C{i}-USDT" for i in range(8)]
    reg = _registry(syms)
    prices = {s: 100.0 for s in syms}
    # monotonic scores: C7 highest ... C0 lowest
    scores = {f"E-C{i}-USDT": float(i - 3.5) for i in range(8)}
    strat = SentimentEdgeStrategy(PortfolioCfg(longs=2, shorts=2, weighting="equal",
                                               target_gross_leverage=2.0, max_name_weight=0.6))
    book = strat.build_book(scores, prices, reg, equity=10_000)

    longs = {s for s, p in book.positions.items() if p.target_contracts > 0}
    shorts = {s for s, p in book.positions.items() if p.target_contracts < 0}
    assert longs == {"E-C7-USDT", "E-C6-USDT"}     # top 2
    assert shorts == {"E-C0-USDT", "E-C1-USDT"}    # bottom 2

    # gross ~= leverage*equity = 20k; net ~= 0
    assert abs(book.realized_gross - 20_000) < 50
    assert abs(book.realized_net) < 1e-6
    assert abs(book.long_notional - book.short_notional) < 1e-6


def test_gross_scales_with_leverage_and_equity():
    syms = [f"E-C{i}-USDT" for i in range(4)]
    reg = _registry(syms)
    prices = {s: 50.0 for s in syms}
    scores = {f"E-C{i}-USDT": float(i) for i in range(4)}
    strat = SentimentEdgeStrategy(PortfolioCfg(longs=1, shorts=1, weighting="equal",
                                               target_gross_leverage=3.0, max_name_weight=1.0))
    book = strat.build_book(scores, prices, reg, equity=5_000)
    assert abs(book.realized_gross - 15_000) < 50   # 3 * 5000


def test_empty_when_universe_too_small():
    reg = _registry(["E-ONLY-USDT"])
    strat = SentimentEdgeStrategy(PortfolioCfg(longs=2, shorts=2))
    book = strat.build_book({"E-ONLY-USDT": 1.0}, {"E-ONLY-USDT": 100.0}, reg, equity=10_000)
    assert book.positions == {}
