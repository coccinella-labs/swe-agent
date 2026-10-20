"""Transient-retry tests for the shared provider transport mixin."""

import json

import pytest

import driver
from driver import Driver, GeminiDriver, OpenRouterDriver

DELA0 = (0.0, 0.0)


def stub_once(drv, script):
    """Feed a scripted list of (status, bytes) or OSError to _send_once."""
    calls = []

    def fake(body: bytes):
        item = script.pop(0) if script else (599, b"")
        calls.append(item)
        if isinstance(item, Exception):
            raise item
        return item

    drv._send_once = fake
    return calls


def resp(status, body=b"{}"):
    return status, body


def test_503_then_200_succeeds_on_second_attempt():
    d = Driver(api_key="k", retry_delays=DELA0)
    calls = stub_once(d, [resp(503, b"busy"), resp(200, json_ok())])
    r = d.chat("s", [{"role": "user", "content": "hi"}])
    assert r.text == "ok"
    assert len(calls) == 2


def json_ok():
    return json.dumps(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    ).encode()


def test_persistent_503_exhausts_attempts_then_surfaces_driver_error():
    d = Driver(api_key="k", retry_delays=DELA0)
    calls = stub_once(d, [resp(503, b"busy")] * 3)
    with pytest.raises(driver.DriverError, match="HTTP 503"):
        d.chat("s", [{"role": "user", "content": "hi"}])
    assert len(calls) == 3


def test_read_timeout_is_retried_then_wrapped():
    d = Driver(api_key="k", retry_delays=DELA0)
    calls = stub_once(
        d,
        [
            OSError("The read operation timed out"),
            OSError("The read operation timed out"),
            OSError("The read operation timed out"),
        ],
    )
    with pytest.raises(driver.DriverError, match="model call failed"):
        d.chat("s", [{"role": "user", "content": "hi"}])
    assert len(calls) == 3


def test_read_timeout_then_success_is_recovered():
    d = GeminiDriver(
        api_key="k",
        prices={"gemini-3.8-flash": {"input": 0, "output": 0}},
        retry_delays=DELA0,
    )
    ok = json.dumps(
        {
            "candidates": [
                {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
    ).encode()
    calls = stub_once(d, [driver.urllib.error.URLError("socket hangup"), resp(200, ok)])
    r = d.chat("s", [{"role": "user", "content": "hi"}])
    assert r.text == "ok"
    assert len(calls) == 2


def test_400_class_is_not_retried():
    d = OpenRouterDriver(api_key="k", retry_delays=DELA0)
    calls = stub_once(d, [resp(400, b'{"error": {"message": "bad"}}')])
    with pytest.raises(driver.DriverError, match="HTTP 400"):
        d.chat("s", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_402_credits_is_not_retried():
    d = Driver(api_key="k", retry_delays=DELA0)
    calls = stub_once(
        d,
        [
            resp(
                400,
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "credit balance",
                        },
                    }
                ).encode(),
            )
        ],
    )
    with pytest.raises(driver.DriverError, match="HTTP 400"):
        d.chat("s", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_positive_delays_shape_the_sleep_calls():
    d = OpenRouterDriver(api_key="k", retry_delays=(2.0, 3.0))
    d._send_once = lambda body: resp(503, b"busy")
    sleeps = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr("driver.time.sleep", lambda s: sleeps.append(s))
    try:
        with pytest.raises(driver.DriverError, match="HTTP 503"):
            d.chat("s", [{"role": "user", "content": "hi"}])
    finally:
        monkey.undo()
    assert sleeps == [2.0, 3.0]


def test_all_three_providers_carry_the_mixin():
    for cls in (Driver, GeminiDriver, OpenRouterDriver):
        assert callable(getattr(cls, "_send", None))
        assert issubclass(cls, driver._TransientRetry)
