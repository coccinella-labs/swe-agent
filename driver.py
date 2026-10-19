"""Minimal Anthropic Messages API client with hard caps.

No third-party runtime dependencies: the HTTP call goes through urllib and
the whole transport is stubbable for offline tests. Cost figures are
estimates from a small price table and are only used to enforce the cost
cap, never shown as authoritative billing.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"

# Dollars per million tokens. Estimates for cap enforcement only; override
# via the prices argument when these drift from the real price sheet.
PRICES = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
}


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


class Driver:
    """Anthropic Messages API client with cumulative cost tracking."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_output_tokens: int = 2048,
        cost_cap_usd: float = 2.0,
        prices: dict | None = None,
        timeout: int = 120,
    ):
        self.model = model or os.environ.get("SWE_AGENT_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_output_tokens = max_output_tokens
        self.cost_cap_usd = cost_cap_usd
        self.prices = prices if prices is not None else dict(PRICES)
        self.timeout = timeout
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

    def _send(self, body: bytes) -> tuple[int, bytes]:
        """The HTTP boundary; the only thing offline tests need to stub."""
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
        except (OSError, urllib.error.URLError) as exc:
            raise DriverError(f"model call failed: {exc}") from exc
