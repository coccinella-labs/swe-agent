"""Tests for the model driver, all offline via a stubbed transport"""

import json

import pytest

import driver
from driver import CapReached, Driver, DriverError


def ok(status=200, stop_reason="end_turn", usage_in=10, usage_out=20):
    raw = json.dumps(
        {
            "content": [{"type": "text", "text": "driving reply"}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
        }
    ).encode()
    return status, raw


@pytest.fixture
def driver_():
    return Driver(
        model="m",
        api_key="k",
        prices={"m": {"input": 1.0, "output": 1.0}},
        cost_cap_usd=0.01,
    )


def test_chat_parses_text_and_usage(driver_):
    driver_._send = lambda body: ok()
    result = driver_.chat("system", [{"role": "user", "content": "go"}])
    assert result.text == "driving reply"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 10
    assert result.output_tokens == 20
    # (10 + 20) tokens at 1 dollar per million
    assert result.cost_estimate == pytest.approx(30 / 1_000_000)


def test_missing_key_refuses_to_call(driver_):
    driver_.api_key = ""
    driver_._send = lambda body: ok()  # must never be reached
    with pytest.raises(DriverError, match="ANTHROPIC_API_KEY"):
        driver_.chat("s", [])


def test_http_error_raises_with_status_and_body(driver_):
    driver_._send = lambda body: (500, b"boom")
    with pytest.raises(DriverError, match="HTTP 500") as info:
        driver_.chat("s", [])
    assert "boom" in str(info.value)


def test_unparseable_response_raises(driver_):
    driver_._send = lambda body: (200, b"<html>not json</html>")
    with pytest.raises(DriverError, match="unparseable"):
        driver_.chat("s", [])


def test_missing_content_raises(driver_):
    driver_._send = lambda body: (200, json.dumps({"usage": {}}).encode())
    with pytest.raises(DriverError):
        driver_.chat("s", [])


def test_cost_cap_stops_after_budget(driver_):
    driver_._send = lambda body: ok()
    driver_.cost_cap_usd = 50 / 1_000_000  # first call costs 30 up to 50
    driver_.chat("s", [])  # 30 tokens, inside the cap
    with pytest.raises(CapReached, match="cost cap"):
        driver_.chat("s", [])  # cumulative 60, over the cap


def test_usage_summary_accumulates(driver_):
    driver_._send = lambda body: ok()
    driver_.chat("s", [])
    summary = driver_.usage_summary()
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 10
    assert summary["output_tokens"] == 20
    assert summary["model"] == "m"


def test_request_body_carries_system_messages_and_caps(driver_):
    seen = {}

    def fake_send(body):
        seen.update(json.loads(body))
        return ok()

    driver_._send = fake_send
    driver_.chat("the system", [{"role": "user", "content": "go"}])
    assert seen["model"] == "m"
    assert seen["max_tokens"] == driver_.max_output_tokens
    assert seen["system"] == "the system"
    assert seen["messages"] == [{"role": "user", "content": "go"}]


def test_unknown_pricing_refuses_noisy_cap(driver_):
    driver_.prices = {}
    driver_._send = lambda body: ok()
    with pytest.raises(DriverError, match="price entry"):
        driver_.chat("s", [])


def test_transport_error_raises_with_detail(driver_, monkeypatch):
    def boom(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(driver.urllib.request, "urlopen", boom)
    with pytest.raises(DriverError, match="connection refused"):
        driver_.chat("s", [])
