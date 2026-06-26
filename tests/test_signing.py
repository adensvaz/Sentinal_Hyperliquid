from sentinel.exchange.client import build_prehash, sign, encode_query, encode_body


def test_sign_matches_published_hmac_sha256_vector():
    # Canonical RFC-style HMAC-SHA256 test vector, lowercase hex.
    msg = "The quick brown fox jumps over the lazy dog"
    assert sign("key", msg) == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"


def test_sign_is_lowercase_hex_64():
    out = sign("secret", "1700000000000GET/fapi/v1/ping")
    assert len(out) == 64
    assert out == out.lower()
    assert all(c in "0123456789abcdef" for c in out)


def test_prehash_get_appends_question_mark_and_query():
    ph = build_prehash("1700000000000", "GET", "/fapi/v1/ticker", "contractName=E-BTC-USDT", "")
    assert ph == "1700000000000GET/fapi/v1/ticker?contractName=E-BTC-USDT"


def test_prehash_get_without_query_has_no_question_mark():
    ph = build_prehash("1700000000000", "GET", "/fapi/v1/ping", "", "")
    assert ph == "1700000000000GET/fapi/v1/ping"


def test_prehash_post_appends_body_no_question_mark():
    ph = build_prehash("1700000000000", "POST", "/fapi/v1/order", "", '{"a":1}')
    assert ph == '1700000000000POST/fapi/v1/order{"a":1}'


def test_encode_query_drops_none_and_is_stable():
    assert encode_query({"contractName": "E-BTC-USDT", "limit": 100, "x": None}) == "contractName=E-BTC-USDT&limit=100"
    assert encode_query(None) == ""


def test_encode_body_compact_and_drops_none():
    assert encode_body({"contractName": "E-BTC-USDT", "volume": 3, "price": None}) == '{"contractName":"E-BTC-USDT","volume":3}'
    assert encode_body(None) == ""
