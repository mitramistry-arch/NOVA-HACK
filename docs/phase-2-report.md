# Phase 2 report — browser identity, BFF, and tenant authorization

**Status: COMPLETE** for the engineering scope of this phase. Deployment is **not** enabled: it needs a
human-selected identity provider and secrets (`AD-1`, `AD-6`). See §7.

| Field | Value |
|---|---|
| Branch | `arena/83a8215b-nova-hack` |
| Phase | 2 of 10 — browser identity, backend-for-frontend, tenant authorization |
| Environment | Python 3.11.2 (isolated venv), Node 22 available (unused this phase), ruff 0.17.2, pytest 8.4.2, PyJWT 2.15.1 |
| Predecessor | Phase 1 report: `docs/phase-1-report.md` |

## 1. What was built

| Area | File | Purpose |
|---|---|---|
| Vendor-neutral OIDC | `nova/identity/oidc.py` | Authorization code + PKCE (S256); `state`/`nonce` validation; ID-token signature/`iss`/`aud`/`exp`/`azp` verification via cached, rotating JWKS; discovery issuer pinning; SSRF-safe endpoint checks |
| Opaque sessions | `nova/identity/session.py` | 32-byte tokens hashed at rest, 8 h absolute + 30 min idle expiry, rotation, revocation (per session and per subject), startup purge, single-use login transactions |
| Roles and permissions | `nova/identity/roles.py`, `models.py` | `viewer`/`editor`/`admin` → granular `Permission` set; immutable `Principal`, `SessionRecord`, `IssuedSession`, `TenantGrant` |
| Server-side resolution | `nova/identity/dependencies.py`, `service.py` | `require_session`, `require_role(permission)`, identity→tenant grant mapping, cookie issuance/clearing, origin and CSRF enforcement, per-session rate limits |
| Same-origin BFF | `nova/api/bff.py` | `/api/bff/{login,callback,whoami,logout}` plus an explicit allowlist of 15 proxied API routes, each with its own permission; server-held tenant credential; client credential/tenant headers never forwarded |
| Config and fail-closed gate | `nova/identity/config.py` | `IdentitySettings.from_env()`; production refuses to start with `NOVA_IDENTITY_MODE=oidc` and any missing/insecure value |
| Wiring | `nova/api/main.py`, `nova/api/runtime.py`, `nova/api/errors.py` | BFF mounted only in OIDC mode; `Runtime.identity`; typed identity errors mapped to 401/403/429/502/503 |
| Tests | `tests/test_identity_{roles,session,oidc,config,bff}.py` | 107 new tests |
| Docs | `docs/browser-identity-bff.md` (new), `production-readiness-matrix.md`, `release-checklist.md`, `README.md`, `frontend/README.md`, `.env.example` | Security model, configuration surface, operator checklist, status reconciliation |

`nova/core/` was **not** modified: identity settings live beside the identity package
(`nova/identity/config.py`) and reuse `nova.core.config.ConfigError` for fail-closed startup errors.

## 2. Exact commands and results

Passed:

```
pytest -q -W error::ResourceWarning                     # 599 passed, 1 warning in 18.7s
pytest tests/test_identity_*.py -q                      # 107 passed (bff 25, config 23, oidc 18, roles 26, session 15)
python -m compileall -q nova benchmark scripts tests    # exit 0
python -m build                                         # wheel + sdist built
python scripts/check_wheel.py dist/*.whl                # OK
# clean venv, runtime-only: pip install dist/*.whl && pip check && import nova.api.main
#   -> "No broken requirements found."  "import ok: Nova API Gateway api_key"
ruff check nova/identity nova/api/bff.py tests/test_identity_*.py                     # All checks passed
ruff format --check nova/identity nova/api/bff.py tests/test_identity_*.py            # 16 files already formatted
git diff --check                                       # clean
```

Failed / unchanged (pre-existing, deliberately not bundled):

```
ruff check .            # 87 errors — identical count to the Phase 1 baseline; none in Phase 2 files
ruff format --check .   # 25 files would be reformatted (all pre-existing; Phase 2 files are formatted)
npm run typecheck / npm test / npm run build   # not re-run this phase; still red from Phase 1 (CI-1/FE-2/FE-3)
```

Not run (and therefore not claimed as passing):

- Any real identity provider. All OIDC and BFF tests use a **locally signed fake provider** over an
  injected `httpx.MockTransport`. No network call, no vendor account, no real credential.
- Browser execution of the login flow (no Playwright/Pyppeteer run; the frontend still targets `/api`).
- Python 3.12/3.13, multi-worker/multi-host behaviour, edge rate limiting, TLS termination.

## 3. Requirements traceability

| Requirement | Where it is satisfied | Test |
|---|---|---|
| 1. Standards-based OIDC, no home-grown passwords, vendor detail behind an interface | `identity/oidc.py` uses only provider metadata/token/JWKS endpoints; no vendor field reaches sessions or roles | `test_identity_oidc.py` (18) |
| 2. Same-origin BFF; server-held tenant credential; no browser key; no browser tenant authority | `api/bff.py` proxy + `service.tenant_credential()` | `test_forged_tenant_headers_and_keys_are_ignored`, `test_two_sessions_are_isolated_across_tenants`, `test_static_api_keys_still_work_for_trusted_clients` |
| 3. Secure/HttpOnly/SameSite cookies, rotation, expiry, logout, CSRF, strict origin, redirect allowlist, no storage of tokens | `identity/session.py`, `identity/service.py` | `test_callback_sets_hardened_cookies_and_whoami_works`, `test_idle_expired_session_is_rejected`, `test_logout_revokes_the_session`, `test_mutations_require_a_matching_csrf_token`, `test_csrf_token_from_another_session_is_rejected`, `test_cross_origin_mutations_are_rejected`, `test_open_redirect_targets_are_ignored` |
| 4. Membership/role checks on every operation; tenant scope derived server-side | `identity/roles.py` + `bff.PROXY_ROUTES` permissions + service layer | `test_role_denial_blocks_ingest_but_allows_questions`, `test_identity_roles.py`, store-layer tenant filters (unchanged) |
| 5. API-key path preserved; fail closed; test/dev bypass opt-in only | `resolve_tenant` untouched; `validate_for_environment` requires complete config in production | `test_static_api_keys_still_work_for_trusted_clients`, `test_production_startup_fails_without_identity_configuration`, `test_identity_config.py` |
| 6. Security tests for anonymous/expired/CSRF/isolation/forged headers/role denial/logout/key non-disclosure/cookie attributes | `tests/test_identity_*.py` | all of the above, plus `test_anonymous_requests_are_rejected`, `test_unknown_or_replayed_state_is_rejected`, `test_session_cookie_is_not_readable_from_javascript` |

Definition of done: no backend key appears in browser assets (no frontend change was made, and the
existing `check:assets` guard is unchanged); every user-facing BFF operation derives tenant scope and
role authorization server-side.

## 4. Defect found and fixed during the phase

The first BFF integration test failed with `session_invalid` on callback. Root cause: `IdentityService
.start_login` generated a `state` value locally while `SessionStore.start_login` generated and stored
its **own** state, so the value sent to the provider was never the value stored. Fixed by making the
session store the single owner of state generation and using its returned value. This is exactly the
class of bug the end-to-end test exists to catch; it would have made every real login fail.

## 5. Design decisions taken (and why)

- **One new dependency: `pyjwt[crypto]>=2.8,<3`.** ID-token verification needs JWT signature checks;
  hand-rolling JWT parsing/verification is a known footgun (algorithm confusion, padding, claim
  handling). PyJWT is the maintained, standards-based choice and reuses the already-present
  `cryptography` package, so no new native build dependency appears. The algorithm allowlist is still
  enforced by our code *and* by PyJWT; `none` is rejected in two places.
- **`NOVA_IDENTITY_MODE` (api_key | oidc) instead of making OIDC mandatory everywhere.** A pure
  service-to-service deployment has no browser surface, so demanding an IdP would break working
  deployments without improving security. In OIDC mode the gate is strict; in api_key mode **no BFF
  route is mounted at all**, so there is no half-open browser login to attack.
- **Static server-side grant map (`NOVA_IDENTITY_USERS` / `NOVA_IDENTITY_GROUPS`)** rather than
  inventing directory integration. Explicit, auditable, and reversible; directory-driven membership
  remains human decision `AD-1`.
- **BFF calls the internal API in-process through ASGI** so the existing middleware, tenant API-key
  auth, validation, and error mapping all still apply. It is not a bypass of the API's own controls.
- **Origin + `Sec-Fetch-Site` + session-bound double-submit CSRF** together, so a missing `Origin`
  (old clients, curl) fails closed rather than silently skipping the check.
- **Revocation is a soft delete** (row marked revoked, purged later) so revocation is immediate but
  sessions cannot be silently revived.

## 6. Residual risks and limitations

- **No real provider has been exercised.** Signature/claim handling is tested against locally signed
  tokens with the same library and the same claim rules, but provider-specific quirks (claim shapes,
  `azp`, group claim naming, key rotation cadence) are unverified.
- **Rate limiting is in-process.** It resets on restart and is per worker; edge limits are still
  required for a public deployment.
- **Sessions live in the deployment's control SQLite database.** Losing it logs everyone out (safe,
  but must be planned), and there is no shared session store for multi-region deployments.
- **`last_seen_at` is written on every authenticated request**, which is correct for idle expiry but
  adds one small write per request.
- **Proxy timeout uses `asyncio.wait_for`.** On timeout the browser gets 504, but the in-process
  handler thread is not force-killed; a slow provider call can still finish in the background.
- **No refresh-token handling, no back-channel logout, no token revocation call** to the provider.
  Logout revokes the local session only (the provider session may persist — documented).
- **The frontend still calls `/api/*`.** Switching `frontend/src/api.ts` to `/api/bff/*` and the
  remaining UI screens are Phase 7 work. Until then the BFF is reachable but not used by the shipped UI.
- **`CI-1` remains open**: repository-wide ruff is still red from pre-existing files. Phase 2 files are
  clean, and I introduced no new violations (87 before, 87 after).
- **Login CSRF is covered by `state`; logout CSRF by the session-bound token.** No other browser state
  is mutated yet.

## 7. What the human must do next

1. **`AD-1` — choose the identity provider** and register a confidential OIDC client; supply issuer,
   client id, and client secret through a secret manager (never chat, never a commit).
2. Add `https://<bff-origin>/api/bff/callback` to that client's redirect allowlist, configure
   `openid`/`email`/`groups` scopes.
3. Generate one backend API key per tenant and set `NOVA_TENANT_API_KEYS` (server-side only).
4. Fill `NOVA_IDENTITY_USERS` (or `NOVA_IDENTITY_GROUPS`) with reviewed people and roles.
5. Run a staging sign-in with a real test user and confirm logout, expiry, and CSRF behaviour.
6. Decide `AD-6` (encryption-key/secret custody) before any production deployment.
7. Approve landing `CI-1` (the pre-existing lint/format sweep) as a separate `chore:` commit so Phase 3
   starts from a green gate.

Full operator checklist: [`browser-identity-bff.md`](browser-identity-bff.md) §8.
