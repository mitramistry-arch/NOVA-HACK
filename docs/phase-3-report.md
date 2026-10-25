# Phase 3 report — AI provider adapters, BYOK, and usage controls

**Status: COMPLETE** (with one human-only item outstanding: `PROV-2`, a recorded run against a real
provider using a real key).

**Branch:** `arena/c25b361e-nova-hack` · **Base:** `bb5d21c` (= `main` after PR #3)
**Scope:** only Phase 3. No unrelated module was modified, no feature or test was removed, and no
security check was weakened.

---

## 1. What was asked, and what was delivered

| Requirement | Delivery |
|---|---|
| Audit existing abstractions, take the smallest compatible extension | The Phase 2 adapter already had SSRF-safe endpoint validation, pinned transport, `follow_redirects=False`, typed errors, `UsageTally`, and prompt budgeting. Kept all of it; added only what was missing (registry, capabilities, retries, usage provenance) |
| Clean adapter interface, provider code isolated | `LLMClient` protocol unchanged. New `nova/providers/registry.py` is the **only** place a `ProviderKind` maps to a class — `ProviderService` no longer imports any concrete adapter |
| BYOK through secure configuration, keys never exposed | Unchanged and re-verified: write-only `SecretStr`, encrypted at rest, masked `****last4`, no key in responses/logs/errors/audit/browser. New tests assert the key is absent from error text, log text, and log record fields after a retry |
| Validate settings, fail clearly | `ProviderSettings` now validates retry windows and price pairs; invalid values fail at the API boundary with `ProviderConfigError` or a 422 |
| Timeouts, bounded retries with backoff, safe error mapping, cancellation where supported | Per-attempt `timeout_s`, `max_attempts` 1–5, exponential backoff with ±25 % jitter, `Retry-After` honoured (capped 30 s), total time budget, only 429/timeout/5xx retried. Cancellation is **documented as not claimed** — the request path is synchronous, so a cancel token would be a claim the code cannot keep |
| Usage/cost metadata only when real; label estimates | `nova/providers/usage.py` labels every call `provider` / `estimate` / `unavailable`. Cost appears only from operator-configured prices and is always `cost_is_estimate=True`. No usage block ⇒ `null`, never `0` |
| Separate generation from embedding capability | `capabilities.py`: `OPENAI_COMPATIBLE` declares `generation` only. `ProviderService` requires `GENERATION` before building; `GET /provider` reports the declared list |
| Deterministic mock adapters, separate from real tests | `tests/provider_fakes.py` (scripted transport, recording sleep, fake clock) — test-only, absent from the wheel. `FakeLLM` stays gated by `NOVA_ALLOW_FAKE_LLM` and rejected in production |
| Documentation | New [`providers.md`](providers.md): supported matrix, BYOK flow, env vars, limits, failure/retry table, usage labelling, extension path |

---

## 2. Changed files

**New**
- `nova/providers/capabilities.py` — `ProviderCapability`, `CAPABILITIES`, `capabilities_for`, `supports`, `require`
- `nova/providers/registry.py` — `AdapterSpec`, `ADAPTERS`, `build_adapter`, `spec_for`, `supported_kinds`
- `nova/providers/retry.py` — `RetryPolicy`, `call_with_retries`, `retry_after_from_error`
- `nova/providers/usage.py` — `UsageSource`, `UsageReport`, `estimate_cost`
- `tests/provider_fakes.py` — deterministic test doubles (test-only)
- `tests/test_provider_adapters.py` — 43 tests
- `docs/providers.md`, `docs/phase-3-report.md` (this file)

**Modified**
- `nova/providers/openai_compat.py` — requests route through `call_with_retries`; injectable `sleep`/`monotonic`; `Retry-After` captured; `last_usage` provenance reset per response
- `nova/providers/service.py` — uses the registry and a capability check instead of a hard-coded class
- `nova/providers/config.py` — retry-budget and price fields with validation; `capabilities` on `ProviderStatus`
- `nova/providers/errors.py` — `ProviderError` carries an optional `retry_after_s`
- `docs/production-readiness-matrix.md` — `PROV-1` closed, `PROV-2` restated as human-only, new §11
- `.env.example` — provider section clarified (no server-wide provider key)

---

## 3. Verification (all commands run in this environment, Python 3.11.2)

| Command | Result |
|---|---|
| `/home/user/.venv-nova/bin/pytest -q -W error::ResourceWarning` | **642 passed**, 1 warning, 24.9 s (599 before Phase 3; +43) |
| `/home/user/.venv-nova/bin/pytest tests/test_provider_adapters.py -q` | **43 passed**, 0.44 s; repeated 3× to confirm determinism (no flakes) |
| `/home/user/.venv-nova/bin/ruff check .` | `All checks passed!` |
| `/home/user/.venv-nova/bin/ruff format --check .` | `135 files already formatted` |
| `/home/user/.venv-nova/bin/python -m compileall -q nova benchmark scripts tests` | exit 0 |
| `/home/user/.venv-nova/bin/python -m build` | `Successfully built nova-0.2.0.tar.gz and nova-0.2.0-py3-none-any.whl` |
| `/home/user/.venv-nova/bin/python scripts/check_wheel.py dist/*.whl` | `OK: … contains the intended distribution`; wheel contains all 9 `nova/providers/*.py` and **no** test files |
| `python -m benchmark.runner --seed 7 --n 30 --split dev --systems baseline,rag --llm fake --limit 30` | exit 0, no system errors (fake-mode scores ~0 by design) |
| `python -m benchmark.validate_inputs` | exit 0 |

### Not run (and why)

- **A real provider round trip** — no provider key exists in this environment, and none may be
  invented. All provider tests use `httpx.MockTransport`. This is `PROV-2`.
- **PostgreSQL / hosted identity / browser E2E** — unchanged from Phase 2; out of scope here.

---

## 4. Behavior changes an operator should know

1. **Retries are now on by default** (`max_attempts=3`). A tenant that was previously failing fast on a
   503 will now spend up to `total_timeout_s` before erroring. Set `max_attempts: 1` to restore the old
   behaviour per tenant.
2. **`GET /provider` returns an extra field** `capabilities`. Additive; existing clients ignore it.
3. **Cost fields are optional and paired.** Configuring only `input_cost_per_1k` is now a validation
   error rather than a silent misreport.
4. **Existing tenants keep working**: stored configs omit the new fields and fall back to their
   defaults (`extra="forbid"` applies to input, and all new fields have defaults).

---

## 5. Residual risks

| Risk | Severity | Note |
|---|---|---|
| No real-provider evidence (`PROV-2`) | **High** | Retry semantics, `Retry-After` parsing, and usage-shape handling are verified against a mock that I wrote to match the documented contract. A real provider may differ. Mitigated by keeping the adapter narrow and the error mapping total |
| Retry multiplies tail latency | Medium | Worst case is bounded by `max_attempts` × `timeout_s` and `total_timeout_s`, but a pathological upstream can hold a request near that envelope. Operators can lower it per tenant |
| Synchronous request path | Medium | No true cancellation; a slow provider occupies a threadpool worker until the timeout. Documented, not hidden |
| `estimate` usage source is unused | Low | The enum member exists for a future deterministic tokenizer; no adapter claims it. Nothing fabricates a count today |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars remain in `Settings` | Low | They only feed the audit scrubber; documented as **not** a configuration path. Dead-knob cleanup (`DOC-3`) is still worth doing |
| Jitter uses a shared `random.Random` | Low | Not security-relevant; it exists to de-synchronize retries |

---

## 6. Human-only actions

1. **`AD-3` — provide a provider key.** Decide which provider/account Nova should use, fund it, and
   record the decision in `docs/architecture-decisions.md`. This is a business/commercial choice, not
   an engineering one.
2. **Execute `PROV-2`** with that key: one successful completion and one recorded failure-mode run
   (e.g. wrong key ⇒ `provider_auth_failed`; forced 429 ⇒ bounded retry then `provider_rate_limited`).
   Paste the sanitized results into `docs/providers.md`.
3. **Confirm the price figures** if cost reporting is wanted; the numbers in the docs are illustrative
   and must come from the operator's own contract.
4. **Decide on a self-hosted model host** if `NOVA_ALLOW_PRIVATE_ENDPOINTS` should be enabled — that
   loosens SSRF protection and deserves an explicit decision.
