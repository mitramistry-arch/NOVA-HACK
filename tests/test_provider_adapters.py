"""Phase 3: adapter registry, retry/backoff, capability separation, and usage provenance.

Every test injects a deterministic transport and a recording sleep: no test contacts a real
provider, and no test waits on a real timer.
"""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from nova.core.llm import LLMClient
from nova.providers import registry
from nova.providers.capabilities import ProviderCapability, capabilities_for, require, supports
from nova.providers.config import (
    ProviderConfigRequest,
    ProviderKind,
    ProviderSettings,
    ProviderStatus,
)
from nova.providers.errors import (
    ProviderAuthError,
    ProviderBudgetError,
    ProviderConfigError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from nova.providers.openai_compat import OpenAICompatibleLLM
from nova.providers.registry import build_adapter, spec_for, supported_kinds
from nova.providers.retry import RetryPolicy, call_with_retries
from nova.providers.usage import UsageReport, UsageSource, estimate_cost
from tests.provider_fakes import (
    FakeClock,
    RecordingSleep,
    ScriptedTransport,
    error_response,
    ok_response,
)

KEY = "sk-test-key-0000000000001234"


def _settings(**overrides: Any) -> ProviderSettings:
    """Build valid provider settings for local tests."""
    base: dict[str, Any] = {"model": "test-model", "retry_base_delay_s": 0.01}
    base.update(overrides)
    return ProviderSettings(**base)


def _adapter(
    script: list[Any], **setting_overrides: Any
) -> tuple[OpenAICompatibleLLM, ScriptedTransport, RecordingSleep]:
    """Build an adapter wired to a scripted transport and a recording sleep."""
    transport = ScriptedTransport(script)
    sleeper = RecordingSleep()
    llm = OpenAICompatibleLLM(
        _settings(**setting_overrides),
        KEY,
        allow_private=True,
        transport=transport.transport(),
        sleep=sleeper,
    )
    return llm, transport, sleeper


# ---- adapter registry ---------------------------------------------------------------------


def test_registry_exposes_the_supported_kinds() -> None:
    """The registry is the single place a kind maps to an adapter."""
    assert supported_kinds() == (ProviderKind.OPENAI_COMPATIBLE,)
    spec = spec_for(ProviderKind.OPENAI_COMPATIBLE)
    assert spec.requires_api_key is True
    assert ProviderCapability.GENERATION in spec.capabilities


def test_unregistered_kind_is_a_typed_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An adapter that is not registered fails as configuration, not as a crash."""
    monkeypatch.delitem(registry.ADAPTERS, ProviderKind.OPENAI_COMPATIBLE)
    with pytest.raises(ProviderConfigError):
        spec_for(ProviderKind.OPENAI_COMPATIBLE)


def test_build_adapter_requires_a_key_when_the_adapter_needs_one() -> None:
    """A missing key is rejected before any network call."""
    with pytest.raises(ProviderConfigError):
        build_adapter(_settings(), None, allow_private=True)


def test_build_adapter_returns_an_llm_client() -> None:
    """The registry-built object satisfies the frozen client protocol."""
    transport = ScriptedTransport([ok_response()])
    llm = build_adapter(_settings(), KEY, allow_private=True, transport=transport.transport())
    assert isinstance(llm, LLMClient)
    assert llm.complete("hello") == "ok"
    closer = getattr(llm, "close", None)
    if callable(closer):
        closer()


# ---- capability separation ----------------------------------------------------------------


def test_generation_and_embeddings_are_separate_capabilities() -> None:
    """The Chat Completions adapter declares generation only, never embeddings."""
    kind = ProviderKind.OPENAI_COMPATIBLE
    assert supports(kind, ProviderCapability.GENERATION) is True
    assert supports(kind, ProviderCapability.EMBEDDINGS) is False
    assert capabilities_for(kind) == frozenset({ProviderCapability.GENERATION})


def test_requiring_embeddings_from_a_generation_only_adapter_fails_clearly() -> None:
    """Asking a generation provider for embeddings is a configuration error with a safe message."""
    with pytest.raises(ProviderConfigError) as excinfo:
        require(ProviderKind.OPENAI_COMPATIBLE, ProviderCapability.EMBEDDINGS)
    message = str(excinfo.value)
    assert "embeddings" in message and KEY not in message


def test_status_reports_real_capabilities(tmp_path: Path) -> None:
    """Provider status exposes declared capabilities so the UI never guesses."""
    from tests.test_phase2_integrations import _req, _runtime

    rt = _runtime(tmp_path, lambda r: ok_response())
    status = rt.providers.configure("t1", _req())
    assert status.capabilities == ["generation"]
    assert "embeddings" not in status.capabilities
    assert KEY not in status.model_dump_json()


def test_build_llm_refuses_a_kind_without_the_generation_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service re-checks capabilities, so a misdeclared kind fails before any call."""
    from nova.providers import capabilities as caps
    from tests.test_phase2_integrations import _req, _runtime

    rt = _runtime(tmp_path, lambda r: ok_response())
    rt.providers.configure("t1", _req())
    monkeypatch.setitem(caps.CAPABILITIES, ProviderKind.OPENAI_COMPATIBLE, frozenset())
    with pytest.raises(ProviderConfigError) as excinfo:
        rt.providers.build_llm("t1")
    assert KEY not in str(excinfo.value)


def test_fake_llm_is_never_a_silent_fallback(tmp_path: Path) -> None:
    """With the dev flag off, an unconfigured tenant raises instead of getting a fake."""
    from tests.test_phase2_integrations import _runtime

    rt = _runtime(tmp_path, lambda r: ok_response())
    with pytest.raises(ProviderNotConfiguredError):
        rt.providers.build_llm("nobody")


def test_fake_llm_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev-only fake flag is rejected by production configuration validation."""
    from nova.core.config import ConfigError, Settings
    from nova.security.secrets import generate_key

    monkeypatch.setenv("NOVA_ENV", "production")
    monkeypatch.setenv("NOVA_ALLOW_FAKE_LLM", "1")
    monkeypatch.setenv("NOVA_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("NOVA_API_KEY", generate_key())
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env().validate_supported()
    assert "NOVA_ALLOW_FAKE_LLM" in str(excinfo.value)


def test_status_defaults_to_no_capabilities_when_unconfigured() -> None:
    """An unconfigured provider advertises nothing."""
    assert ProviderStatus(configured=False).capabilities == []


# ---- retry policy ---------------------------------------------------------------------------


def test_retry_policy_rejects_unsafe_values() -> None:
    """A policy cannot be configured to hot-loop or to retry forever."""
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=6)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_s=2.0, max_delay_s=1.0)
    with pytest.raises(ValueError):
        RetryPolicy(total_timeout_s=0)
    with pytest.raises(ValueError):
        RetryPolicy(jitter_ratio=1.5)


def test_only_transient_failures_are_retryable() -> None:
    """Auth, malformed-response, budget, and config errors are never retried."""
    policy = RetryPolicy()
    assert policy.is_retryable(ProviderRateLimitError("429")) is True
    assert policy.is_retryable(ProviderTimeoutError("slow")) is True
    assert policy.is_retryable(ProviderUnavailableError("503")) is True
    for error in (
        ProviderAuthError("401"),
        ProviderResponseError("shape"),
        ProviderBudgetError("budget"),
        ProviderConfigError("config"),
    ):
        assert policy.is_retryable(error) is False


def test_backoff_is_exponential_and_capped() -> None:
    """Delays grow exponentially, respect the cap, and add no jitter when disabled."""
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=2.0, jitter_ratio=0.0, total_timeout_s=60.0)
    assert [policy.delay_for(a) for a in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 2.0]


def test_retry_after_header_overrides_backoff() -> None:
    """A provider-supplied Retry-After wins, within a sane cap."""
    policy = RetryPolicy(base_delay_s=0.5, jitter_ratio=0.0)
    assert policy.delay_for(1, retry_after_s=3.0) == 3.0
    assert policy.delay_for(1, retry_after_s=10_000.0) == policy.delay_for(1, retry_after_s=30.0)


def test_jitter_stays_within_bounds_and_is_reproducible() -> None:
    """Jitter never exceeds the configured spread and is deterministic for a fixed seed."""
    import random

    first = RetryPolicy(base_delay_s=1.0, max_delay_s=1.0, jitter_ratio=0.25, _rng=random.Random(7))
    second = RetryPolicy(
        base_delay_s=1.0, max_delay_s=1.0, jitter_ratio=0.25, _rng=random.Random(7)
    )
    left = [first.delay_for(a) for a in range(1, 6)]
    right = [second.delay_for(a) for a in range(1, 6)]
    assert left == right
    assert all(0.75 <= value <= 1.25 for value in left)


# ---- retry behaviour through the real adapter ----------------------------------------------


def test_transient_failure_is_retried_then_succeeds() -> None:
    """A 503 followed by success returns the completion and stops retrying."""
    llm, transport, sleeper = _adapter(
        [error_response(503), ok_response()], retry_base_delay_s=0.01
    )
    assert llm.complete("hello") == "ok"
    assert transport.calls == 2
    assert len(sleeper.delays) == 1


def test_retries_are_bounded_by_max_attempts() -> None:
    """Persistent failure raises the typed error after exactly max_attempts attempts."""
    llm, transport, sleeper = _adapter(
        [error_response(503)], max_attempts=3, retry_base_delay_s=0.01
    )
    with pytest.raises(ProviderUnavailableError):
        llm.complete("hello")
    assert transport.calls == 3
    assert len(sleeper.delays) == 2


def test_max_attempts_of_one_disables_retrying() -> None:
    """A single-attempt policy makes exactly one HTTP call."""
    llm, transport, _ = _adapter([error_response(503)], max_attempts=1)
    with pytest.raises(ProviderUnavailableError):
        llm.complete("hello")
    assert transport.calls == 1


def test_rate_limit_retry_after_is_honoured_end_to_end() -> None:
    """A 429 carrying Retry-After waits that long instead of the computed backoff."""
    llm, transport, sleeper = _adapter(
        [error_response(429, retry_after="7"), ok_response()], retry_base_delay_s=0.01
    )
    assert llm.complete("hello") == "ok"
    assert transport.calls == 2
    assert sleeper.delays == [7.0]


@pytest.mark.parametrize("status", [401, 403, 400, 422])
def test_non_transient_statuses_are_not_retried(status: int) -> None:
    """Auth and client-error responses fail immediately: retrying would burn quota."""
    llm, transport, sleeper = _adapter([error_response(status)])
    with pytest.raises((ProviderAuthError, ProviderResponseError)):
        llm.complete("hello")
    assert transport.calls == 1
    assert sleeper.delays == []


def test_timeout_is_retried_and_then_reported() -> None:
    """Transport timeouts are transient, and the final error stays typed."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    llm, transport, sleeper = _adapter([boom], max_attempts=2, retry_base_delay_s=0.01)
    with pytest.raises(ProviderTimeoutError):
        llm.complete("hello")
    assert transport.calls == 2
    assert sleeper.delays


def test_total_time_budget_stops_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large backoff cannot exceed the configured total retry budget."""
    llm, transport, _ = _adapter(
        [error_response(503)],
        max_attempts=5,
        retry_base_delay_s=5.0,
        retry_max_delay_s=5.0,
        total_timeout_s=1.0,
    )
    with pytest.raises(ProviderUnavailableError):
        llm.complete("hello")
    assert transport.calls == 1


def test_prompt_budget_is_checked_before_any_retry() -> None:
    """A prompt over budget fails once, without touching the network."""
    llm, transport, sleeper = _adapter([ok_response()], max_prompt_tokens=1)
    with pytest.raises(ProviderBudgetError):
        llm.complete("x" * 100)
    assert transport.calls == 0
    assert sleeper.delays == []


def test_retry_never_leaks_the_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retries do not echo the credential or the upstream body, in errors or logs."""
    llm, _, _ = _adapter([error_response(503)], max_attempts=2, retry_base_delay_s=0.01)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderUnavailableError) as excinfo:
        llm.complete("hello")
    assert KEY not in str(excinfo.value)
    assert KEY not in caplog.text
    assert "must not leak" not in str(excinfo.value)
    assert "must not leak" not in caplog.text
    retry_records = [r for r in caplog.records if "retrying provider call" in r.getMessage()]
    assert retry_records, "a retry should be logged for observability"
    assert all(KEY not in str(record.__dict__) for record in retry_records)


def test_backoff_actually_sleeps_between_attempts() -> None:
    """The sleeps requested are non-decreasing for the default exponential policy."""
    policy = RetryPolicy(base_delay_s=0.01, jitter_ratio=0.0, total_timeout_s=60.0)
    sleeper = RecordingSleep()
    clock = FakeClock()
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ProviderUnavailableError("503")
        return "ok"

    result = call_with_retries(
        flaky, policy, sleep=clock.sleep_with(sleeper), monotonic=clock.monotonic
    )
    assert result == "ok"
    assert attempts["n"] == 3
    assert sleeper.delays == sorted(sleeper.delays)


# ---- usage and cost provenance -------------------------------------------------------------


def test_provider_usage_is_recorded_when_reported() -> None:
    """Reported token counts are exposed with provider provenance."""
    llm, _, _ = _adapter([ok_response()])
    llm.complete("hello")
    assert llm.last_usage.source is UsageSource.PROVIDER
    assert llm.last_usage.prompt_tokens == 11
    assert llm.last_usage.completion_tokens == 7
    assert llm.last_usage.total_tokens == 18
    assert llm.last_usage.cost_usd is None
    assert llm.last_usage.cost_is_estimate is False


def test_missing_usage_block_reports_unavailable_not_zero() -> None:
    """A response without usage yields 'unavailable', never invented counts."""
    body = {"choices": [{"message": {"content": "ok"}}]}
    llm, _, _ = _adapter([ok_response(body)])
    assert llm.complete("hello") == "ok"
    assert llm.last_usage.source is UsageSource.UNAVAILABLE
    assert llm.last_usage.prompt_tokens is None
    assert llm.last_usage.total_tokens is None
    assert llm.usage.reported_calls == 0


def test_non_integer_usage_is_treated_as_unavailable() -> None:
    """Malformed usage values are ignored rather than coerced into numbers."""
    body = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": "11", "completion_tokens": 7},
    }
    llm, _, _ = _adapter([ok_response(body)])
    llm.complete("hello")
    assert llm.last_usage.source is UsageSource.UNAVAILABLE


def test_cost_requires_configured_prices() -> None:
    """No prices means no cost figure: absence is not zero."""
    assert estimate_cost(1000, 1000, input_cost_per_1k=None, output_cost_per_1k=None) is None
    assert estimate_cost(0, 0, input_cost_per_1k=None, output_cost_per_1k=1.0) is None


def test_configured_prices_produce_a_labelled_estimate() -> None:
    """Operator-supplied prices give a cost that is always labelled an estimate."""
    report = UsageReport.from_provider(2000, 1000, input_cost_per_1k=0.5, output_cost_per_1k=1.5)
    assert report.cost_usd == pytest.approx(2.5)
    assert report.cost_is_estimate is True
    assert report.source is UsageSource.PROVIDER


def test_adapter_records_cost_only_when_prices_are_set() -> None:
    """The adapter path honours the same rule as the helper."""
    llm, _, _ = _adapter([ok_response()], input_cost_per_1k=0.01, output_cost_per_1k=0.02)
    llm.complete("hello")
    assert llm.last_usage.cost_usd is not None
    assert llm.last_usage.cost_is_estimate is True


def test_half_configured_prices_are_rejected() -> None:
    """Configuring only one price would silently misreport cost, so it is invalid."""
    with pytest.raises(ValidationError):
        ProviderSettings(model="m", input_cost_per_1k=0.5)


# ---- configuration validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_attempts": 0},
        {"max_attempts": 9},
        {"retry_base_delay_s": -1},
        {"retry_base_delay_s": 5.0, "retry_max_delay_s": 1.0},
        {"total_timeout_s": 0},
        {"output_cost_per_1k": -0.5},
    ],
)
def test_invalid_retry_and_price_settings_fail_validation(overrides: dict[str, Any]) -> None:
    """Invalid budgets fail clearly at the boundary instead of at call time."""
    with pytest.raises(ValidationError):
        ProviderSettings(model="m", **overrides)


def test_retry_settings_are_accepted_through_the_config_request() -> None:
    """Operators can tune retries per tenant through the existing BYOK payload."""
    request = ProviderConfigRequest(
        model="m",
        api_key=KEY,
        max_attempts=2,
        retry_base_delay_s=0.5,
        retry_max_delay_s=1.5,
        total_timeout_s=20,
        input_cost_per_1k=0.15,
        output_cost_per_1k=0.6,
    )
    settings = request.settings_only()
    assert settings.max_attempts == 2
    assert settings.total_timeout_s == 20
    assert KEY not in settings.model_dump_json()
