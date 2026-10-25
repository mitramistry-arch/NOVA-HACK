# Provider adapters, BYOK, and usage controls

**Phase 3 deliverable.** Status: **COMPLETE** in source; one item stays human-only (`PROV-2`,
a recorded run against a real provider with a real key). See [`production-readiness-matrix.md`](production-readiness-matrix.md)
and [`phase-3-report.md`](phase-3-report.md).

This page documents what the code actually does. Where an older document disagrees, **the source is
truth** and this page wins.

---

## 1. Supported providers

| Kind (`ProviderKind`) | Adapter module | Protocols | Capabilities | Status |
|---|---|---|---|---|
| `openai_compatible` | `nova/providers/openai_compat.py` | OpenAI Chat Completions (`POST {base_url}/chat/completions`) | `generation` | Implemented |

Anything that speaks the OpenAI Chat Completions shape can be configured through `base_url`: the
hosted OpenAI API, Azure-style gateways that expose the same path, or a self-hosted server
(vLLM, LiteLLM, Ollama's OpenAI shim, …).

**Not supported, deliberately:** Anthropic Messages, Google Gemini, Bedrock, and any "agent" API
(`agent_id` is rejected by validation). They are not implemented, so no capability is claimed.
Adding a provider is a new adapter module plus one registry declaration (§8), not a config change.

### Capabilities are declared per adapter, not assumed

`nova/providers/capabilities.py` maps each kind to the capabilities it *genuinely implements*:

```python
CAPABILITIES = {
    ProviderKind.OPENAI_COMPATIBLE: frozenset({ProviderCapability.GENERATION}),
}
```

`generation` and `embeddings` are separate capabilities. The Chat Completions adapter does **not**
declare `embeddings`: OpenAI's embeddings endpoint exists, but this adapter does not implement it,
and production embeddings are a Phase 5 concern. `ProviderService.build_llm()` calls
`require(kind, GENERATION)` and `GET /provider` reports the declared list, so a client never has to
guess. Requesting an undeclared capability fails as `ProviderConfigError` before any network call.

---

## 2. Bring Your Own Key (BYOK)

Keys are **per tenant**, **write-only**, and **encrypted at rest**. There is no server-wide provider
key: `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are read by `Settings.from_env()` only to add them to
the audit redaction set — setting them does **not** configure a provider. Configure through the API:

```bash
# Configure (or replace) the tenant's provider. The key is stored encrypted, never echoed back.
curl -sS -X PUT https://your-host/provider \
  -H "Authorization: Bearer $NOVA_TENANT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "kind": "openai_compatible",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "timeout_s": 30,
        "max_output_tokens": 1024,
        "max_prompt_tokens": 16000,
        "max_attempts": 3,
        "retry_base_delay_s": 0.25,
        "retry_max_delay_s": 2.0,
        "total_timeout_s": 30,
        "input_cost_per_1k": 0.15,
        "output_cost_per_1k": 0.60,
        "api_key": "sk-..."
      }'
```

```bash
curl -sS https://your-host/provider -H "Authorization: Bearer $NOVA_TENANT_API_KEY"
# {"configured": true, "kind": "openai_compatible", "model": "gpt-4o-mini",
#  "has_api_key": true, "api_key_mask": "****a1b2", "capabilities": ["generation"], ...}

curl -sS -X POST https://your-host/provider/test -H "Authorization: Bearer $NOVA_TENANT_API_KEY"
# {"ok": true, "code": "ok", "message": "...", "latency_ms": 412.3}

curl -sS -X DELETE -i https://your-host/provider -H "Authorization: Bearer $NOVA_TENANT_API_KEY"
# 204 — configuration and stored key both removed
```

`POST /provider/test` with a request body tests a *candidate* configuration (one tiny completion);
the candidate key is used for that call only and is **never persisted**.

### Where the key can and cannot appear

| Location | Key present? | Mechanism |
|---|---|---|
| Inbound `PUT /provider` body | yes, transitively | `SecretStr`, write-only |
| Secret store (`nova_control.db`) | encrypted | `AesGcmCipher` from `NOVA_ENCRYPTION_KEY` |
| `GET /provider` / any API response | **no** | `ProviderStatus` has no key field; mask is `****last4` |
| Frontend bundle / `localStorage` | **no** | never sent to the browser; BFF proxies are same-origin |
| Logs | **no** | only counts/status codes are logged; audit scrubber also matches key values |
| Error messages | **no** | `ProviderError.public_message` is a fixed safe string; upstream bodies are dropped |
| Audit payloads | **no** | provider audit events record action + outcome only |
| Tests / fixtures / docs | **no** | tests use obviously fake `sk-test-…` values |

Encryption key material (`NOVA_ENCRYPTION_KEY`, 32-byte base64) is required in production and is
validated at startup. Key rotation is supported through `NOVA_ENCRYPTION_KEYS_PREVIOUS`; see
`docs/architecture-decisions.md` (`AD-3`).

---

## 3. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `NOVA_ENV` | `production` | `production`/`prod` enables fail-closed checks |
| `NOVA_ENCRYPTION_KEY` | — | **required in production**; encrypts stored provider keys |
| `NOVA_ENCRYPTION_KEYS_PREVIOUS` | — | optional previous keys for rotation |
| `NOVA_CONTROL_DB_PATH` | `nova_control.db` | control-plane store holding per-tenant config + secrets |
| `NOVA_DB_PATH` | `nova.db` | per-tenant memory store |
| `NOVA_ALLOW_PRIVATE_ENDPOINTS` | off | permits a private/loopback `base_url` (self-hosted models). Off by default; enabling it widens SSRF exposure |
| `NOVA_ALLOW_FAKE_LLM` | off | dev/test only: lets an *unconfigured* tenant use `FakeLLM`. Refused in production |
| `NOVA_API_KEY` / `NOVA_TENANT_API_KEYS` | — | tenant authentication (not a provider key) |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | — | **not a provider configuration path**; only added to audit redaction |
| `NOVA_LLM_PROVIDER` | `fake` | legacy knob; any value other than `fake` fails startup validation |

There is no `PROVIDER_*` env var: provider selection, model, and key are per-tenant data, not process
configuration. This is what makes multi-tenant BYOK possible without a process restart.

---

## 4. Limits

Per-tenant, validated by `ProviderSettings` (pydantic, `extra="forbid"`):

| Field | Default | Range | Why |
|---|---|---|---|
| `timeout_s` | 30 | 1–120 | per-HTTP-attempt timeout |
| `max_output_tokens` | 1024 | 1–8192 | caps spend per call |
| `max_prompt_tokens` | 16000 | 1–200000 | prompt-budget guard, checked *before* the request |
| `max_attempts` | 3 | 1–5 | total attempts including the first (1 = no retry) |
| `retry_base_delay_s` | 0.25 | 0–5 | first backoff delay |
| `retry_max_delay_s` | 2.0 | 0–30, ≥ base | per-delay cap |
| `total_timeout_s` | 30 | >0 – 300 | total retry budget, measured with a monotonic clock |
| `input_cost_per_1k` / `output_cost_per_1k` | unset | 0–1000 | optional price metadata; **both or neither** |

`base_url` goes through an SSRF check (`nova/security/net.py`): HTTPS only, no credentials in the
URL, no redirects (`follow_redirects=False`), and the validated address is pinned for the transport
so DNS cannot be rebound between check and connect. Private/loopback addresses are rejected unless
`NOVA_ALLOW_PRIVATE_ENDPOINTS` is on.

---

## 5. Failure behavior

### Retries

`nova/providers/retry.py` retries **only** transient failures:

| Outcome | Error | Retried? |
|---|---|---|
| HTTP 429 | `ProviderRateLimitError` | yes (honours `Retry-After`, capped at 30 s) |
| HTTP 5xx, 3xx | `ProviderUnavailableError` | yes |
| connect/read timeout | `ProviderTimeoutError` | yes |
| HTTP 401/403 | `ProviderAuthError` | **no** — retrying would burn quota for a wrong key |
| other 4xx (400/404/422) | `ProviderResponseError` | **no** |
| malformed/empty completion | `ProviderResponseError` | **no** |
| prompt over budget | `ProviderBudgetError` | **no** — fails before the request |
| bad config, unregistered kind, missing capability | `ProviderConfigError` | **no** |

Delay for attempt *n* is `min(base_delay_s * 2**(n-1), max_delay_s)` with ±`25 %` jitter, so
concurrent tenants do not retry in lockstep. A provider-supplied `Retry-After` (numeric form) wins
over the computed delay. Before each sleep, the elapsed time plus the next delay is compared with
`total_timeout_s`; if that would overshoot, the last error is raised instead of sleeping.

Worst-case latency for the defaults: 3 attempts × 30 s timeout, with the sleep budget bounded by
`total_timeout_s` — the call cannot outlive that envelope by more than one in-flight attempt.

### Safe errors

Every failure reaches the caller as a `ProviderError` subclass with a stable `code` and a fixed
`public_message`. Upstream bodies, headers, URLs, and key material are never included. The internal
exception message is used for logs only and still carries no credential.

### Cancellation

**Not claimed.** Nova's request path is synchronous (`def` endpoints on a threadpool); there is no
request-scoped cancellation token to honour, and inventing a partial one would be a safety claim the
code cannot keep. What bounds a slow call today: the per-attempt timeout, the total retry budget, and
the client disconnect (Starlette stops using the response). If the project moves to async endpoints,
cancellation becomes implementable by threading `asyncio.CancelledError` through `_post` — the retry
layer already treats unknown exceptions as non-retryable, so cancellation would propagate immediately.

---

## 6. Usage and cost: labelled, never invented

`nova/providers/usage.py` records provenance with every count:

| `UsageSource` | Meaning |
|---|---|
| `provider` | the response contained a usable `usage` block (integer `prompt_tokens` and `completion_tokens`) |
| `estimate` | reserved for a documented, deterministic tokenizer-based estimate; **not used by any adapter today** |
| `unavailable` | the provider reported nothing, or reported something malformed |

`OpenAICompatibleLLM.last_usage` is reset to `unavailable` at the start of every parse, so a later
response that omits `usage` never inherits an earlier call's numbers. `UsageTally` (fed into the
`/ask` response) only counts calls where the provider actually reported usage; a run of 10 calls
where 3 report usage shows 3.

Cost is computed **only** when the tenant configured both prices, and is always flagged
`cost_is_estimate=True`: the numbers come from operator-supplied price metadata, not from the
provider. No price, no cost — absence is `null`, never `0`. This is also why configuring only one of
the two prices is a validation error.

`benchmark/counting_llm.py` keeps its own `tokens_estimated` label for the benchmark harness; a
benchmark estimate is not provider usage and is not model-quality evidence.

---

## 7. Testing: fakes are test-only

| Artifact | Location | Shipped? |
|---|---|---|
| `ScriptedTransport`, `RecordingSleep`, `FakeClock` | `tests/provider_fakes.py` | no — `tests/` is excluded from the wheel |
| `FakeLLM` | `nova/core/llm.py` | in the wheel, but only reachable when `NOVA_ALLOW_FAKE_LLM` is set, and refused in production by `Settings.validate_supported()` |

Provider tests inject `httpx.MockTransport` and a recording sleep function, so they are
deterministic and take milliseconds — no network, no real timers. `tests/test_provider_adapters.py`
covers the registry, capability separation, retry/backoff bounds, `Retry-After`, non-retryable
statuses, usage provenance, cost labelling, and key non-disclosure. Real-provider tests are a
separate, human-executed category (`PROV-2`) because they need a funded key.

---

## 8. Adding an adapter (the intended extension path)

1. Create `nova/providers/<name>.py` implementing the `LLMClient` protocol
   (`complete`, plus `usage` and `last_usage`), using `call_with_retries` and the shared
   `ProviderError` types so retry/error mapping stays uniform.
2. Add the kind to `ProviderKind` **and** its capabilities to `CAPABILITIES`. Declare only what the
   adapter really implements.
3. Register it in `nova/providers/registry.py` (`ADAPTERS`). No caller changes: the service, routes,
   and tests depend on the kind.
4. Add a deterministic mock-transport test plus a documented `PROV-2` human run against the real API.

Nothing outside `nova/providers/` should import a concrete adapter class.
