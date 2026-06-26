import pytest

from sentinel.exchange.client import _interpret, KoinbayAPIError


def test_plain_success_passthrough():
    assert _interpret(200, {"orderId": "123"}) == {"orderId": "123"}


def test_futures_success_envelope_code_zero():
    body = {"code": "0", "msg": "success", "data": None}
    assert _interpret(200, body) == body


def test_spot_business_error_int_code():
    with pytest.raises(KoinbayAPIError) as e:
        _interpret(200, {"code": -1121, "msg": "Invalid symbol."})
    assert e.value.kind == "business"
    assert e.value.code == -1121


def test_futures_business_error_string_code():
    with pytest.raises(KoinbayAPIError) as e:
        _interpret(200, {"code": "-1022", "succ": False, "msgData": None, "msg": "bad sign"})
    assert e.value.kind == "business"


def test_infra_spring_envelope():
    with pytest.raises(KoinbayAPIError) as e:
        _interpret(404, {"timestamp": 1, "status": 404, "error": "Not Found",
                         "message": "no route", "path": "/nope"})
    assert e.value.kind == "infra"
    assert e.value.code == 404


def test_spot_query_order_status_field_is_not_infra():
    # spot GET /order legitimately has a "status" field but is NOT an infra error
    body = {"symbol": "btcusdt", "status": "CANCELED", "orderId": 1}
    assert _interpret(200, body) == body
