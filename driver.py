"""Model provider clients with hard cap discipline.

Two providers sit behind the same driver contract: Anthropic's Messages
API and Google's Gemini REST generateContent. No third-party runtime
dependencies: the HTTP calls go through urllib and the whole transport
is stubbable for offline tests. Cost figures are estimates from a small
price table and are only used to enforce the cost cap, never shown as
authoritative billing.
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_KEY_FILE = Path("/tmp/coccinella-labs/gemini_key")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_KEY_FILE = Path("/tmp/coccinella-labs/openrouter_key")

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

# Dollars per million tokens. Estimates for cap enforcement only; override
# via the prices argument when these drift from the real price sheet.
PRICES = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
    # Google AI Studio standard paid tier, fetched 2026-10-20; thought
    # tokens bill as output ("output price (including thinking tokens)").
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},
}

DRIVER_PROVIDERS = {
    "anthropic": "Driver",
    "gemini": "GeminiDriver",
    "openrouter": "OpenRouterDriver",
}


def provider_name() -> str:
    """Provider from SWE_AGENT_PROVIDER; empty falls back to anthropic."""
    raw = os.environ.get("SWE_AGENT_PROVIDER", "")
    stripped = raw.strip().lower()
    return stripped or "anthropic"


def get_provider_class(name: str):
    """Resolve a provider name to its client class; None when unknown."""
    cls_name = DRIVER_PROVIDERS.get(name)
    if cls_name is None:
        return None
    return globals()[cls_name]


class DriverError(Exception):
    """Model call could not be completed."""


class CapReached(DriverError):
    """A configured cap was exhausted and further calls are refused."""


@dataclass
class ChatResult:
    text: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cost_estimate: float


class _TransientRetry:
    """Shared transient-failure retry for the provider transports.

    Retries 429/5xx responses and network-level OSErrors (including
    read timeouts) with fixed delays, then surfaces the last failure
    through the driver error shapes as before. 400-class errors are
    not transient and return immediately.
    """

    retry_delays: tuple = (5.0, 15.0)

    def _send(self, body: bytes) -> tuple[int, bytes]:
        attempts = len(self.retry_delays) + 1
        last_status, last_raw, last_exc = 0, b"", None
        for attempt in range(attempts):
            if attempt:
                time.sleep(self.retry_delays[attempt - 1])
            try:
                status, raw = self._send_once(body)
            except (OSError, urllib.error.URLError) as exc:
                last_exc = exc
                continue
            if status in TRANSIENT_STATUSES and attempt < attempts - 1:
                last_status, last_raw = status, raw
                continue
            return status, raw
        if last_exc is not None:
            raise DriverError(f"model call failed: {last_exc}") from last_exc
        return last_status, last_raw


class Driver(_TransientRetry):
    """Anthropic Messages API client with cumulative cost tracking."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_output_tokens: int = 2048,
        cost_cap_usd: float = 2.0,
        prices: dict | None = None,
        timeout: int = 120,
        retry_delays: tuple | None = None,
    ):
        self.model = model or os.environ.get("SWE_AGENT_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_output_tokens = max_output_tokens or 2048
        self.cost_cap_usd = cost_cap_usd
        self.prices = prices if prices is not None else dict(PRICES)
        self.timeout = timeout
        self.retry_delays = retry_delays or (5.0, 15.0)
        self._calls = 0
        self._spent = 0.0
        self._input_tokens = 0
        self._output_tokens = 0

    def chat(self, system, messages) -> ChatResult:
        """One Messages API call; accumulates usage and enforces the cap."""
        if not self.api_key:
            raise DriverError("no api key: set ANTHROPIC_API_KEY or pass api_key")
        body = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": system,
            "messages": messages,
        }
        status, raw = self._send(json.dumps(body).encode())
        if status != 200:
            head = raw[:200].decode(errors="replace")
            raise DriverError(f"model call returned HTTP {status}: {head}")
        try:
            data = json.loads(raw)
            content = data["content"]
            text = "".join(
                block["text"] for block in content if block.get("type") == "text"
            )
            usage = data.get("usage", {})
            stop_reason = data.get("stop_reason")
        except (ValueError, KeyError, TypeError) as exc:
            raise DriverError(f"unparseable model response: {exc}") from exc
        in_tokens = int(usage.get("input_tokens", 0))
        out_tokens = int(usage.get("output_tokens", 0))
        cost = self._cost(in_tokens, out_tokens)
        if cost is None:
            raise DriverError(
                f"no price entry for model {self.model!r}; pass prices so "
                "the cost cap can be enforced"
            )
        self._input_tokens += in_tokens
        self._output_tokens += out_tokens
        self._calls += 1
        self._spent += cost
        if self.cost_cap_usd is not None and self._spent > self.cost_cap_usd:
            raise CapReached(
                f"cost cap of {self.cost_cap_usd} USD exceeded after about "
                f"{self._spent:.4f} USD"
            )
        return ChatResult(
            text=text,
            stop_reason=stop_reason,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_estimate=cost,
        )

    def usage_summary(self) -> dict:
        return {
            "provider": "anthropic",
            "model": self.model,
            "calls": self._calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cost_estimate": round(self._spent, 6),
        }

    def _cost(self, in_tokens: int, out_tokens: int) -> float | None:
        price = self.prices.get(self.model)
        if not price:
            return None
        return (in_tokens * price["input"] + out_tokens * price["output"]) / 1_000_000

    def _send_once(self, body: bytes) -> tuple[int, bytes]:
        """One HTTP attempt; the retry loop lives in _TransientRetry."""
        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (OSError, urllib.error.URLError):
            raise


_PLACEHOLDER_KEYS = {"", "gemini_api_key", "placeholder", "your-api-key-here"}


def _resolve_gemini_key(api_key, key_file):
    """Environment first, then the on-disk keyfile; refuse placeholders.

    Fail closed at construction: an empty or obviously placeholder key
    raises here, before any run can be attempted with it.
    """
    key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    if key:
        return key
    path = Path(key_file) if key_file else GEMINI_KEY_FILE
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        raise DriverError(
            f"no gemini api key: set GEMINI_API_KEY or write {path} (mode 0600)"
        ) from None
    except OSError as exc:
        raise DriverError(f"gemini key file unreadable: {exc}") from exc
    if not raw or raw.lower() in _PLACEHOLDER_KEYS:
        raise DriverError(f"placeholder or empty gemini key at {path}")
    return raw


class GeminiDriver(_TransientRetry):
    """Gemini generateContent client with the same cap discipline."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_output_tokens: int = 2048,
        cost_cap_usd: float = 2.0,
        prices: dict | None = None,
        timeout: int = 120,
        retry_delays: tuple | None = None,
        key_file: str | None = None,
    ):
        self.model = model or os.environ.get("SWE_AGENT_MODEL", GEMINI_DEFAULT_MODEL)
        self.api_key = _resolve_gemini_key(api_key, key_file)
        self.max_output_tokens = max_output_tokens or 2048
        self.cost_cap_usd = cost_cap_usd
        self.prices = prices if prices is not None else dict(PRICES)
        self.timeout = timeout
        self.retry_delays = retry_delays or (5.0, 15.0)
        self._calls = 0
        self._spent = 0.0
        self._input_tokens = 0
        self._output_tokens = 0

    def chat(self, system, messages) -> ChatResult:
        """One generateContent call; accumulates usage and enforces the cap."""
        role_map = {"user": "user", "assistant": "model"}
        contents = []
        for message in messages:
            role = role_map.get(message.get("role"))
            if role is None:
                raise DriverError(
                    f"unsupported message role {message.get('role')!r} "
                    "for the gemini provider"
                )
            contents.append(
                {"role": role, "parts": [{"text": message.get("content", "")}]}
            )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.max_output_tokens},
        }
        status, raw = self._send(json.dumps(body).encode())
        if status != 200:
            head = raw[:200].decode(errors="replace")
            raise DriverError(f"model call returned HTTP {status}: {head}")
        try:
            data = json.loads(raw)
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
            usage = data.get("usageMetadata", {})
            stop_reason = data["candidates"][0].get("finishReason")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DriverError(f"unparseable model response: {exc}") from exc
        in_tokens = int(usage.get("promptTokenCount", 0))
        # Thought tokens bill at the output rate per the pricing page.
        out_tokens = int(usage.get("candidatesTokenCount", 0)) + int(
            usage.get("thoughtsTokenCount", 0)
        )
        cost = self._cost(in_tokens, out_tokens)
        if cost is None:
            raise DriverError(
                f"no price entry for model {self.model!r}; pass prices so "
                "the cost cap can be enforced"
            )
        self._input_tokens += in_tokens
        self._output_tokens += out_tokens
        self._calls += 1
        self._spent += cost
        if self.cost_cap_usd is not None and self._spent > self.cost_cap_usd:
            raise CapReached(
                f"cost cap of {self.cost_cap_usd} USD exceeded after about "
                f"{self._spent:.4f} USD"
            )
        return ChatResult(
            text=text,
            stop_reason=stop_reason,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_estimate=cost,
        )

    def usage_summary(self) -> dict:
        return {
            "provider": "gemini",
            "model": self.model,
            "calls": self._calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cost_estimate": round(self._spent, 6),
        }

    def _cost(self, in_tokens: int, out_tokens: int) -> float | None:
        price = self.prices.get(self.model)
        if not price:
            return None
        return (in_tokens * price["input"] + out_tokens * price["output"]) / 1_000_000

    def _send_once(self, body: bytes) -> tuple[int, bytes]:
        """One HTTP attempt; the retry loop lives in _TransientRetry."""
        request = urllib.request.Request(
            GEMINI_API_URL.format(model=self.model),
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-goog-api-key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (OSError, urllib.error.URLError):
            raise


_OPENROUTER_PLACEHOLDER_KEYS = {
    "",
    "openrouter_api_key",
    "placeholder",
    "your-api-key-here",
}


def _resolve_openrouter_key(api_key, key_file):
    """Environment first, then the on-disk keyfile; refuse placeholders."""
    key = (api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
    if key:
        return key
    path = Path(key_file) if key_file else OPENROUTER_KEY_FILE
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        raise DriverError(
            f"no openrouter api key: set OPENROUTER_API_KEY or write {path} (mode 0600)"
        ) from None
    except OSError as exc:
        raise DriverError(f"openrouter key file unreadable: {exc}") from exc
    if not raw or raw.lower() in _OPENROUTER_PLACEHOLDER_KEYS:
        raise DriverError(f"placeholder or empty openrouter key at {path}")
    return raw


class OpenRouterDriver(_TransientRetry):
    """OpenRouter chat-completions client with the same cap discipline.

    Cap accounting is response-reported: usage.cost in credits is the
    authoritative estimate, and a router whose per-call model (and
    pricing) changes admits no static price table. A response without
    a cost fails closed instead of guessing.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_output_tokens: int | None = 4096,
        cost_cap_usd: float = 2.0,
        prices: dict | None = None,
        timeout: int = 120,
        retry_delays: tuple | None = None,
        key_file: str | None = None,
    ):
        self.model = model or os.environ.get(
            "SWE_AGENT_MODEL", OPENROUTER_DEFAULT_MODEL
        )
        self.api_key = _resolve_openrouter_key(api_key, key_file)
        self.max_output_tokens = max_output_tokens or 4096
        self.cost_cap_usd = cost_cap_usd
        self.prices = prices if prices is not None else dict(PRICES)
        self.timeout = timeout
        self.retry_delays = retry_delays or (5.0, 15.0)
        self._calls = 0
        self._spent = 0.0
        self._input_tokens = 0
        self._output_tokens = 0
        self.last_routed_model: str | None = None

    def chat(self, system, messages) -> ChatResult:
        """One chat-completions call; accumulates usage and enforces the cap."""
        body_messages = [{"role": "system", "content": system}]
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant"}:
                raise DriverError(
                    f"unsupported message role {role!r} for the openrouter provider"
                )
            body_messages.append({"role": role, "content": message.get("content", "")})
        body = {
            "model": self.model,
            "messages": body_messages,
            "max_tokens": self.max_output_tokens,
        }
        status, raw = self._send(json.dumps(body).encode())
        if status != 200:
            head = raw[:200].decode(errors="replace")
            raise DriverError(f"model call returned HTTP {status}: {head}")
        try:
            data = json.loads(raw)
            message = data["choices"][0]["message"]
            text = message.get("content") or ""
            finish = data["choices"][0].get("finish_reason")
            self.last_routed_model = data.get("model")
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DriverError(f"unparseable model response: {exc}") from exc
        if not text and finish == "length":
            raise DriverError(
                f"routed model {self.last_routed_model!r} emitted no content "
                f"with finish_reason=length; raise max_output_tokens"
            )
        if not "cost" in usage:
            raise DriverError(
                "response carried no usage.cost; the openrouter provider "
                "is response-reported and cannot enforce a cap without it"
            )
        in_tokens = int(usage.get("prompt_tokens", 0))
        out_tokens = int(usage.get("completion_tokens", 0))
        cost = float(usage["cost"])
        self._input_tokens += in_tokens
        self._output_tokens += out_tokens
        self._calls += 1
        self._spent += cost
        if self.cost_cap_usd is not None and self._spent > self.cost_cap_usd:
            raise CapReached(
                f"cost cap of {self.cost_cap_usd} USD exceeded after about "
                f"{self._spent:.4f} USD"
            )
        return ChatResult(
            text=text,
            stop_reason=finish,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_estimate=cost,
        )

    def usage_summary(self) -> dict:
        return {
            "provider": "openrouter",
            "model": self.model,
            "routed_model": self.last_routed_model,
            "calls": self._calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cost_estimate": round(self._spent, 6),
        }

    def _send_once(self, body: bytes) -> tuple[int, bytes]:
        """One HTTP attempt; the retry loop lives in _TransientRetry."""
        request = urllib.request.Request(
            OPENROUTER_API_URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
                "x-title": "coccinella-labs-swe-agent",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (OSError, urllib.error.URLError):
            raise
