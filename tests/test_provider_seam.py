"""Provider seam tests: selection, payload mapping, usage, caps, key handling.

Everything is offline: the HTTP boundary is stubbed at ``_send``.
"""

import json
import subprocess as sp

import pytest

import driver
from agent import SWEAgent
from driver import GeminiDriver


def payload(text="ok", prompt=10, candidates=3, thoughts=61, finish="STOP"):
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": finish}
        ],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": candidates,
            "thoughtsTokenCount": thoughts,
        },
    }


def stub_send(monkeydrv, status=200, data=None):
    """Replace the transport; capture the parsed request body."""
    captured = {}
    payload_bytes = json.dumps(data if data is not None else payload()).encode()

    def fake(body: bytes):
        captured["body"] = json.loads(body)
        return status, payload_bytes

    monkeydrv._send = fake
    return captured


@pytest.fixture
def solve_repo(no_config, tmp_path):
    """An agent with a real git clone 'fx' holding one buggy file."""
    clone = tmp_path / "coccinella-labs" / "fx"
    (clone / ".git").mkdir(parents=True)
    (clone / "calc.py").write_text("def add(a, b):\n    return a - b  # known bug\n")
    for cmd in (
        ["git", "-C", str(clone), "init", "-q"],
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "add",
            "-A",
        ],
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
    ):
        sp.run(cmd, check=False, capture_output=True)
    return SWEAgent("t", tmp_base=str(tmp_path)), clone


def task(**overrides):
    base = {"repo": "fx", "operation": "solve", "instruction": "fix add"}
    base.update(overrides)
    return base


def make_gemini(**overrides):
    defaults = {
        "api_key": "k" * 10,
        "prices": {"gemini-3.8-flash": {"input": 0.75, "output": 3.75}},
    }
    defaults.update(overrides)
    return GeminiDriver(**defaults)


# --- provider selection -----------------------------------------------------


def test_provider_defaults_to_anthropic(monkeypatch):
    monkeypatch.delenv("SWE_AGENT_PROVIDER", raising=False)
    assert driver.provider_name() == "anthropic"
    assert driver.get_provider_class("anthropic") is driver.Driver


def test_provider_normalizes_case_and_whitespace(monkeypatch):
    monkeypatch.setenv("SWE_AGENT_PROVIDER", "  Gemini ")
    assert driver.provider_name() == "gemini"
    assert driver.get_provider_class("gemini") is driver.GeminiDriver


def test_unknown_provider_is_a_task_error(solve_repo, monkeypatch):
    a, _ = solve_repo
    monkeypatch.setenv("SWE_AGENT_PROVIDER", "openai")
    result = a.execute_task(task())
    assert result["status"] == "error"
    assert "unknown SWE_AGENT_PROVIDER" in result["error"]
    assert "openai" in result["error"]


def test_make_driver_selects_gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("SWE_AGENT_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "k" * 8)
    a = SWEAgent("t", tmp_base=str(tmp_path))
    d = a._make_driver({"max_output_tokens": 512, "cost_cap_usd": 0.5})
    assert isinstance(d, GeminiDriver)
    assert d.max_output_tokens == 512
    assert d.cost_cap_usd == 0.5


# --- payload mapping --------------------------------------------------------


def test_chat_maps_messages_to_gemini_shape():
    d = make_gemini()
    captured = stub_send(d)
    d.chat(
        "fix bugs",
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}],
    )
    body = captured["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "fix bugs"}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "done"}]},
    ]
    assert body["generationConfig"] == {"maxOutputTokens": 2048}


def test_chat_rejects_unmapped_roles():
    d = make_gemini()
    with pytest.raises(driver.DriverError, match="unsupported message role"):
        d.chat("s", [{"role": "system", "content": "nope"}])


def test_usage_counts_thought_tokens_as_output():
    d = make_gemini()
    stub_send(d, data=payload(prompt=10, candidates=3, thoughts=61))
    result = d.chat("s", [{"role": "user", "content": "hi"}])
    assert result.input_tokens == 10
    assert result.output_tokens == 64
    expected = (10 * 0.75 + 64 * 3.75) / 1_000_000
    assert result.cost_estimate == pytest.approx(expected)
    assert result.text == "ok"
    assert result.stop_reason == "STOP"


def test_http_error_becomes_driver_error():
    d = make_gemini()
    stub_send(d, status=400, data={"error": {"message": "bad"}})
    with pytest.raises(driver.DriverError, match="HTTP 400"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_unparseable_response_becomes_driver_error():
    d = make_gemini()
    stub_send(d, status=200, data={"candidates": []})
    with pytest.raises(driver.DriverError, match="unparseable"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_missing_price_entry_fails_closed():
    d = make_gemini(prices={})
    stub_send(d)
    with pytest.raises(driver.DriverError, match="no price entry"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_cost_cap_enforced():
    d = make_gemini(cost_cap_usd=0.00001)
    stub_send(d)
    with pytest.raises(driver.CapReached, match="cost cap"):
        d.chat("s", [{"role": "user", "content": "hi"}])


def test_usage_summary_carries_provider():
    d = make_gemini()
    assert d.usage_summary()["provider"] == "gemini"


# --- key resolution ---------------------------------------------------------


def test_missing_key_everywhere_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(driver, "GEMINI_KEY_FILE", tmp_path / "absent")
    with pytest.raises(driver.DriverError, match="no gemini api key"):
        GeminiDriver()


def test_placeholder_key_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    keyfile = tmp_path / "gemini_key"
    keyfile.write_text("placeholder")
    monkeypatch.setattr(driver, "GEMINI_KEY_FILE", keyfile)
    with pytest.raises(driver.DriverError, match="placeholder or empty"):
        GeminiDriver()


def test_key_file_fallback_reads_contents(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    keyfile = tmp_path / "gemini_key"
    keyfile.write_text("real-key-material")
    monkeypatch.setattr(driver, "GEMINI_KEY_FILE", keyfile)
    d = GeminiDriver(prices={"gemini-3.8-flash": {"input": 1, "output": 1}})
    assert d.api_key == "real-key-material"


def test_env_key_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    keyfile = tmp_path / "gemini_key"
    keyfile.write_text("file-key")
    monkeypatch.setattr(driver, "GEMINI_KEY_FILE", keyfile)
    d = GeminiDriver()
    assert d.api_key == "env-key"


def test_default_model_resolves_to_gemini_3_8_flash(monkeypatch):
    monkeypatch.delenv("SWE_AGENT_MODEL", raising=False)
    assert driver.GEMINI_DEFAULT_MODEL == "gemini-3.8-flash"
