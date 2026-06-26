from sentinel.exchange.contracts import ContractRegistry, ContractSpec

RAW = [
    {"symbol": "E-BTC-USDT", "type": "E", "marginCoin": "USDT", "multiplier": 0.0001,
     "multiplierCoin": "BTC", "pricePrecision": 1, "minOrderVolume": 1,
     "maxMarketVolume": 5000000, "minLever": 0, "maxLever": 150, "status": 1},
    {"symbol": "E-ETH-USDT", "type": "E", "marginCoin": "USDT", "multiplier": 0.001,
     "multiplierCoin": "ETH", "pricePrecision": 2, "minOrderVolume": 1,
     "maxMarketVolume": 1000000, "minLever": 0, "maxLever": 100, "status": 0},   # inactive
    {"symbol": "S-BTC-USDT", "type": "S", "marginCoin": "EXUSD", "multiplier": 0.0001,
     "multiplierCoin": "BTC", "pricePrecision": 1, "minOrderVolume": 1,
     "maxMarketVolume": 5000000, "minLever": 0, "maxLever": 50, "status": 1},    # non-USDT
]


def test_active_usdt_filters_status_and_type():
    reg = ContractRegistry(RAW)
    active = {s.name for s in reg.active_usdt()}
    assert active == {"E-BTC-USDT"}  # ETH inactive (status 0), S non-USDT


def test_notional_to_contracts_btc():
    spec = ContractRegistry(RAW).get("E-BTC-USDT")
    # 1 contract = 0.0001 BTC; at price 60000 that's $6 notional.
    assert spec.notional_to_contracts(600, 60000) == 100
    assert spec.notional_to_contracts(-600, 60000) == -100   # sign preserved


def test_below_minimum_returns_zero():
    spec = ContractRegistry(RAW).get("E-BTC-USDT")
    # $3 < one contract ($6) -> below min order volume -> dropped
    assert spec.notional_to_contracts(3, 60000) == 0


def test_contracts_to_notional_roundtrip():
    spec = ContractRegistry(RAW).get("E-BTC-USDT")
    assert spec.contracts_to_notional(100, 60000) == 600.0


def test_round_price_to_precision():
    spec = ContractRegistry(RAW).get("E-BTC-USDT")  # pricePrecision 1 -> step 0.1
    assert spec.round_price(60315.33) == 60315.3


def test_max_market_volume_cap():
    spec = ContractRegistry(RAW).get("E-BTC-USDT")
    huge = spec.notional_to_contracts(10**12, 60000)
    assert huge == 5_000_000  # capped at maxMarketVolume
