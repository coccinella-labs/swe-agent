"""OpenRouter driver tests; transport stubbed at _send, everything offline."""

import json

import pytest
from test_provider_seam import solve_repo, task  # noqa: F401

import driver
from agent import SWEAgent
from driver import OpenRouterDriver


def payload(
    text="ok",
    cost=0.0001,
    prompt=42,
    completion=7,
    finish="stop",
    routed="deepseek/deepseek-v4.1-flash-1017",
):
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": finish}
        ],
        "model": routed,
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cost": cost,
        },
    }


def make_openrouter(**overrides):
    defaults = {"api_key": "k" * 10}
    defaults.update(overrides)
    return OpenRouterDriver(**defaults)


def stub_send(drv, status=200, data=None):
    captured = {}
    body_bytes = json.dumps(data if data is not None else payload()).encode()

    def fake(body: bytes):
        captured["body"] = json.loads(body)
        return status, body_bytes

    drv._send = fake
    return captured


# --- payload mapping --------------------------------------------------------


def test_chat_keeps_native_roles_and_adds_system():
    d = make_openrouter()
    captured = stub_send(d)
    d.chat(
        "fix bugs",
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}],
    )
    messages = captured["body"]["messages"]
    assert messages[0] == {"role": "system", "content": "fix bugs"}
    assert messages[1:] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ]
    assert captured["body"]["model"] == "openrouter/auto"
    assert captured["body"]["max_tokens"] == 4096


def test_chat_rejects_unmapped_roles():
    d = make_openrouter()
    with pytest.raises(driver.DriverError, match="unsupported message role"):
        d.chat("s", [{"role": "model", "content": "nope"}])


def test_usage_cost_is_the_authoritative_estimate():
    d = make_openrouter()
    stub_send(d, data=payload(cost=0.0002, prompt=35, completion=8))
    result = d.chat("s", [{"role": "user", "content": "hi"}])
    assert result.cost_estimate == 0.0002
    assert result.input_tokens == 35
    assert result.output_tokens == 8
    assert result.stop_reason == "stop"
    assert d.last_routed_model == "deepseek/deepseek-v4.1-flash-1017"


def test_usage_summary_records_routed_model():
    d = make_openrouter()
    stub_send(d)
    d.chat("s", [{"role": "user", "content": "hi"}])
    summary = d.usage_summary()
    assert summary["provider"] == "openrouter"
    assert summary["routed_model"] == "deepseek/deepseek-v4.1-flash-1017"


def test_cumulative_cost_accumulates_across_calls():
    d = make_openrouter()
    stub_send(d, data=payload(cost=0.3))
    d.chat("s", [{"role": "user", "content": "hi"}])
    d.chat("s", [{"role": "user", "content": "hi"}])
    assert d.usage_summary()["cost_estimate"] == pytest.approx(0.6)
    assert d.usage_summary()["calls"] == 2


def test_cost_cap_enforced():
    d = make_openrouter(cost_cap_usd=0.5)
    stub_send(d, data=payload(cost=0.6))
    with pytest.raises(driver.CapReached, match="cost cap"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_missing_cost_fails_closed():
    d = make_openrouter()
    stub_send(
        d,
        status=200,
        data={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    with pytest.raises(driver.DriverError, match="no usage.cost"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_length_with_empty_content_fails_fast():
    d = make_openrouter()
    stub_send(d, data=payload(text=None, finish="length", cost=0.5))
    with pytest.raises(driver.DriverError, match="no content"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_http_error_becomes_driver_error():
    d = make_openrouter()
    stub_send(
        d,
        status=429,
        data={"error": {"code": 429, "message": "rate limited; retry with backoff"}},
    )
    with pytest.raises(driver.DriverError, match="HTTP 429"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_bad_response_shape_fails_closed():
    d = make_openrouter()
    stub_send(d, status=200, data={"choices": []})
    with pytest.raises(driver.DriverError, match="unparseable"):
        d.chat("s", [{"role": "user", "content": "hi"}])


# --- key resolution ---------------------------------------------------------


def test_missing_key_everywhere_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(driver, "OPENROUTER_KEY_FILE", tmp_path / "absent")
    with pytest.raises(driver.DriverError, match="no openrouter api key"):
        OpenRouterDriver()


def test_placeholder_key_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    keyfile = tmp_path / "openrouter_key"
    keyfile.write_text("placeholder")
    monkeypatch.setattr(driver, "OPENROUTER_KEY_FILE", keyfile)
    with pytest.raises(driver.DriverError, match="placeholder or empty"):
        OpenRouterDriver()


def test_key_file_fallback_reads_contents(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    keyfile = tmp_path / "openrouter_key"
    keyfile.write_text("or-real-key")
    monkeypatch.setattr(driver, "OPENROUTER_KEY_FILE", keyfile)
    d = OpenRouterDriver()
    assert d.api_key == "or-real-key"


def test_env_key_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    keyfile = tmp_path / "openrouter_key"
    keyfile.write_text("file-key")
    monkeypatch.setattr(driver, "OPENROUTER_KEY_FILE", keyfile)
    assert OpenRouterDriver().api_key == "env-key"


# --- selection --------------------------------------------------------------


def test_get_provider_class_resolves_all_three():
    assert driver.get_provider_class("openrouter") is driver.OpenRouterDriver
    assert driver.get_provider_class("gemini") is driver.GeminiDriver
    assert driver.get_provider_class("anthropic") is driver.Driver


def test_make_driver_selects_openrouter(tmp_path, monkeypatch):
    monkeypatch.setenv("SWE_AGENT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k" * 8)
    a = SWEAgent("t", tmp_base=str(tmp_path))
    d = a._make_driver({})
    assert isinstance(d, OpenRouterDriver)
    assert d.model == "openrouter/auto"
    assert d.max_output_tokens == 4096
