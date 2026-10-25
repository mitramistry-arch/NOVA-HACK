"""OpenAI-compatible Chat Completions adapter implementing the LLMClient protocol."""

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from nova.core.llm import UsageTally
from nova.providers.config import ProviderSettings
from nova.providers.errors import (
    ProviderAuthError,
    ProviderBudgetError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from nova.providers.retry import RetryPolicy, call_with_retries
from nova.providers.usage import UsageReport
from nova.security.net import (
    PinnedHTTPTransport,
    UnsafeEndpointError,
    resolve_public_addresses,
    validate_https_url,
)

logger = logging.getLogger("nova.providers")


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a numeric Retry-After header; HTTP-date forms are ignored rather than guessed."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


class OpenAICompatibleLLM:
    """LLMClient over POST {base_url}/chat/completions; the API key never leaves this object."""

    def __init__(
        self,
        settings: ProviderSettings,
        api_key: str,
        *,
        allow_private: bool = False,
        transport: httpx.BaseTransport | None = None,
        max_output_tokens: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Validate the endpoint (SSRF rules) and prepare an HTTP client without redirects."""
        try:
            self._base_url = validate_https_url(
                settings.effective_base_url,
                allow_private=allow_private,
                resolve=False,
            )
            if transport is None and not allow_private:
                host = urlsplit(self._base_url).hostname
                if host is None:
                    raise UnsafeEndpointError("provider endpoint has no host")
                addresses = resolve_public_addresses(host)
                if not addresses:
                    raise UnsafeEndpointError("provider endpoint has no vetted address")
                transport = PinnedHTTPTransport({host: addresses})
        except UnsafeEndpointError as e:
            raise ProviderConfigError(f"unsafe provider endpoint: {e}") from e
        self._settings = settings
        self._api_key = api_key
        self._max_output = min(
            max_output_tokens or settings.max_output_tokens, settings.max_output_tokens
        )
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_s, connect=min(5.0, settings.timeout_s)),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        self.usage = UsageTally()
        self.last_usage = UsageReport.unavailable()
        self._sleep = sleep
        self._monotonic = monotonic
        self.retry_policy = RetryPolicy(
            max_attempts=settings.max_attempts,
            base_delay_s=settings.retry_base_delay_s,
            max_delay_s=settings.retry_max_delay_s,
            total_timeout_s=settings.total_timeout_s,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def complete(self, prompt: str, *, json: bool = False) -> str:
        """Send a chat completion, retrying transient failures within the configured budget."""
        if len(prompt) // 3 + 1 > self._settings.max_prompt_tokens:
            raise ProviderBudgetError("prompt exceeds the configured prompt token budget")
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_output,
            "temperature": 0,
        }
        if json:
            payload["response_format"] = {"type": "json_object"}
        return call_with_retries(
            lambda: self._post(payload),
            self.retry_policy,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _post(self, payload: dict[str, Any]) -> str:
        """Perform one HTTP attempt; transient failures surface as typed provider errors."""
        try:
            resp = self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"timeout: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailableError(f"transport error: {type(e).__name__}") from e
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> str:
        self.last_usage = UsageReport.unavailable()
        status = resp.status_code
        retry_after = _retry_after_seconds(resp)
        if status in (401, 403):
            raise ProviderAuthError(f"HTTP {status}")
        if status == 429:
            raise ProviderRateLimitError("HTTP 429", retry_after_s=retry_after)
        if status >= 500 or 300 <= status < 400:
            raise ProviderUnavailableError(f"HTTP {status}", retry_after_s=retry_after)
        if status >= 400:
            raise ProviderResponseError(f"HTTP {status}")
        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderResponseError("unexpected response shape") from e
        if not isinstance(content, str):
            raise ProviderResponseError("response content is not text")
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if isinstance(pt, int) and isinstance(ct, int):
                self.usage.add(pt, ct)
                self.last_usage = UsageReport.from_provider(
                    pt,
                    ct,
                    input_cost_per_1k=self._settings.input_cost_per_1k,
                    output_cost_per_1k=self._settings.output_cost_per_1k,
                )
        return content
