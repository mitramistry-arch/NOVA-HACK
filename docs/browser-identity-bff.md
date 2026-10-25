# Browser identity, BFF, and tenant authorization (Phase 2)

Status: **implemented and tested in source**; deployment still requires human configuration of an
identity provider (see the checklist at the end). This document covers the security model, the exact
configuration surface, and what remains human-owned.

Related: [`production-readiness-matrix.md`](production-readiness-matrix.md) (`SEC-1`, `TEST-1`),
[`architecture-decisions.md`](architecture-decisions.md) (`AD-1`, `AD-2`).

---

## 1. Two authentication paths, deliberately separate

| Caller | Credential | Code path |
|---|---|---|
| Trusted service / CLI / backend integration | `X-API-Key` — `NOVA_API_KEY` (default tenant) or `NOVA_TENANT_API_KEYS` (key → tenant) | unchanged: `nova/api/runtime.py:resolve_tenant` |
| Browser (hosted UI) | `__Host-nova_session` opaque session cookie | `nova/api/bff.py` (only mounted when `NOVA_IDENTITY_MODE=oidc`) |

The BFF **calls the internal API** with a server-held tenant credential. The browser never sees that
credential, and a browser-supplied `X-Tenant-ID` or `X-API-Key` is stripped rather than forwarded.

`NOVA_IDENTITY_MODE=api_key` (the default) mounts no browser routes at all — `/api/bff/*` returns
404 — so an API-only deployment keeps exactly its previous behaviour.

## 2. Request flow

```
Browser ──GET /api/bff/login?next=/workspace──► 302 to provider (state, nonce, PKCE S256)
Browser ──GET /api/bff/callback?code&state────► exchange code (client secret + verifier)
                                                verify id_token: signature, iss, aud, exp, nonce, azp
                                                map subject/email/groups -> tenant + role (server-side)
                                                create session -> Set-Cookie (session + CSRF)
Browser ──POST /api/bff/ingest───────────────► cookie + Origin + Sec-Fetch-Site + CSRF + role check
                                                in-process call to POST /ingest with X-API-Key(tenant)
```

## 3. Security properties and where they are enforced

| Property | Implementation |
|---|---|
| Vendor-neutral OIDC | `nova/identity/oidc.py` uses only OpenID Provider metadata, token, and JWKS endpoints. Any conformant provider works; nothing vendor-specific appears in session, role, or API code |
| Authorization code + PKCE (S256) | `code_challenge` from `code_challenge_for()`; verifier stored server-side per login and sent only to the token endpoint |
| `state` / `nonce` | `state` is single-use, server-stored, 5-minute TTL, hashed at rest; `nonce` is compared in constant time |
| ID-token verification | PyJWT signature check with the provider's JWKS; `alg` must be in `NOVA_OIDC_ALLOWED_ALGS` (`none` rejected by config *and* by the algorithm check); `iss`, `aud`, `exp`, `iat`, `sub` required; `azp` checked when the audience is multi-valued; discovery metadata issuer must equal the configured issuer |
| JWKS rotation | Cached for `NOVA_OIDC_JWKS_CACHE_SECONDS`; an unknown `kid` triggers at most one refetch per 10 s |
| Opaque sessions | 32-byte `secrets` token; only `sha256` stored (`browser_sessions.token_hash`); raw value exists only in the cookie |
| Expiry | Absolute 8 h and idle 30 min by default (`NOVA_SESSION_ABSOLUTE_SECONDS`, `NOVA_SESSION_IDLE_SECONDS`); expired rows are deleted on use and purged at startup |
| Revocation / rotation | `revoke()` marks the row revoked (immediate rejection), `rotate()` replaces token + CSRF secret, `revoke_subject()` drops every session for a subject |
| Cookie posture | `__Host-nova_session`: `Secure; HttpOnly; SameSite=Lax; Path=/` and no `Domain`. `__Host-nova_csrf`: `Secure; SameSite=Lax; Path=/`, readable by same-origin JS by design. Non-`__Host-` names are used only when `NOVA_SESSION_COOKIE_SECURE=false`, which production rejects |
| CSRF | Double submit bound to the session: `X-CSRF-Token` header must equal the CSRF cookie **and** match the session's stored hash. Applies to every POST/PUT/DELETE |
| Origin checks | Mutations require `Origin` exactly equal to `NOVA_BFF_ORIGIN`; `Sec-Fetch-Site`, when present, must be `same-origin` or `none` |
| Open redirect | `?next=` accepts only a same-origin absolute path (`^/(?!/)…`); anything else falls back to `/` |
| Rate limits | `NOVA_BFF_LOGIN_RATE_PER_MINUTE` per peer address, `NOVA_BFF_MUTATION_RATE_PER_MINUTE` per session. In-process only — see §7 |
| Tenant scope | Derived only from the session row (`tenant_id`), never from a request header. Storage queries filter by tenant inside the store layer |
| Roles | `viewer` = ask/verify/audit read; `editor` = + ingest; `admin` = + provider, storage, export, delete |
| Proxy allowlist | Explicit `PROXY_ROUTES` table; each entry has its own permission. `/docs`, `/openapi.json`, `/readyz`, and anything unlisted return 404 |
| Response hygiene | Every BFF response sets `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`; internal `Set-Cookie` headers are never forwarded |
| Fail closed | `NOVA_IDENTITY_MODE=oidc` in production requires issuer, client id/secret, redirect URI, BFF origin, HTTPS URLs, secure cookies, and at least one grant; otherwise startup raises `ConfigError` |

## 4. Configuration

All settings are read by `nova/identity/config.py` (`IdentitySettings.from_env`). They are additive:
`nova/core/config.py` was not modified.

| Variable | Purpose |
|---|---|
| `NOVA_IDENTITY_MODE` | `api_key` (default, no BFF) or `oidc` |
| `NOVA_OIDC_ISSUER` | Provider issuer URL; discovery at `<issuer>/.well-known/openid-configuration` |
| `NOVA_OIDC_CLIENT_ID` / `NOVA_OIDC_CLIENT_SECRET` | Confidential client credentials (secret from a secret manager) |
| `NOVA_OIDC_REDIRECT_URI` | Must be `https://<bff-origin>/api/bff/callback` |
| `NOVA_OIDC_SCOPES` | Must include `openid` |
| `NOVA_BFF_ORIGIN` | Public origin, e.g. `https://app.example.com` |
| `NOVA_IDENTITY_USERS` | `identity:tenant:role` grants, comma separated (subject or email) |
| `NOVA_IDENTITY_GROUPS` | `group:tenant:role` grants resolved from the group claim |
| `NOVA_OIDC_GROUPS_CLAIM` / `NOVA_OIDC_EMAIL_CLAIM` | Claim names (defaults `groups` / `email`) |
| `NOVA_SESSION_COOKIE_SECURE` | Must stay `true` outside local http development |
| `NOVA_SESSION_ABSOLUTE_SECONDS` / `NOVA_SESSION_IDLE_SECONDS` | 28800 / 1800 by default |
| `NOVA_BFF_LOGIN_RATE_PER_MINUTE` / `NOVA_BFF_MUTATION_RATE_PER_MINUTE` | 10 / 60 by default |
| `NOVA_BFF_PROXY_TIMEOUT_SECONDS` | 60 |
| `NOVA_OIDC_ALLOWED_ALGS` | `RS256,ES256,PS256` by default; `none` is refused |
| `NOVA_TENANT_API_KEYS` | **Required for a browser deployment**: one server-held key per tenant the BFF may call |

A browser deployment additionally needs `NOVA_TENANT_API_KEYS` (or `NOVA_API_KEY` for the default
tenant only). Without a credential for the granted tenant, login fails with
`tenant_not_provisioned` rather than issuing a session that cannot work.

## 5. Endpoints

| Route | Method | Notes |
|---|---|---|
| `/api/bff/login` | GET | 302 to the provider; rate limited |
| `/api/bff/callback` | GET | Sets cookies, 303 to `next` |
| `/api/bff/whoami` | GET | `{authenticated, subject, email, tenant_id, role, permissions, session_expires_at}` |
| `/api/bff/logout` | POST | CSRF-protected, revokes the session, 204 |
| `/api/bff/ingest` | POST | `ingest` |
| `/api/bff/ask`, `/api/bff/verify` | POST | `query` |
| `/api/bff/audit/{id}` | GET | `audit.read` |
| `/api/bff/provider`, `/provider/test` | GET/PUT/DELETE/POST | `provider.manage` |
| `/api/bff/storage`, `/storage/test`, `/storage/initialize` | GET/PUT/DELETE/POST | `storage.manage` |
| `/api/bff/data/export` | GET | `data.export` |
| `/api/bff/data` | DELETE | `data.delete` |

The frontend still calls `/api/*` today; switching `frontend/src/api.ts` to the `/api/bff/*` paths is
Phase 7 work (frontend wiring), not a backend change. The Vite dev proxy is unchanged and remains
loopback-only; it is **not** a production gateway.

## 6. What the deployment must still provide

- TLS termination and a single public origin matching `NOVA_BFF_ORIGIN`.
- A secret manager for `NOVA_OIDC_CLIENT_SECRET`, `NOVA_ENCRYPTION_KEY`, and the tenant API keys.
- Edge rate limiting, request logging without cookies, and egress rules to the provider.
- Session storage backups: sessions live in the control SQLite database
  (`NOVA_CONTROL_DB_PATH`); losing it logs everyone out, which is safe but should be planned.

## 7. Known limitations (do not overstate)

- Rate limiting is **per process**. Multi-worker or multi-host deployments need an edge limiter.
- Sessions are stored in the deployment's control database. There is no shared session store for a
  multi-region deployment yet.
- Only the authorization-code flow with a confidential client is implemented; there is no refresh
  token handling, back-channel logout, or token revocation endpoint integration.
- Subject/email/group → tenant mapping is static configuration. Directory-driven membership
  (`AD-1`) is still a human decision; the current map is deliberately explicit and auditable.
- The BFF forwards only the allowlisted routes above; new API routes are invisible to browsers until
  they are added to `PROXY_ROUTES` with a permission.
- No CSRF-protected account-management UI exists yet; `/api/bff/whoami` and `/logout` are the whole
  browser session surface.

## 8. Human setup checklist (not performed by the agent)

1. **Choose an identity provider** (`AD-1`) and register a confidential OIDC web client.
2. Add the redirect URI `https://<bff-origin>/api/bff/callback` to the client allowlist.
3. Enable the `openid`, `email`, and (if group mapping is used) `groups` scopes/claims.
4. Provide `NOVA_OIDC_ISSUER`, `NOVA_OIDC_CLIENT_ID`, `NOVA_OIDC_CLIENT_SECRET`,
   `NOVA_OIDC_REDIRECT_URI`, and `NOVA_BFF_ORIGIN` through the deployment's secret manager.
5. Generate one backend API key per tenant, store it in the secret manager as
   `NOVA_TENANT_API_KEYS`, and never place it in browser code or a `VITE_*` variable.
6. Fill `NOVA_IDENTITY_USERS` (or `NOVA_IDENTITY_GROUPS`) with the real people who may use each
   workspace and their roles.
7. Confirm production startup fails when each of those values is missing (this is intentional).
8. Test the flow in staging with a real (non-production) user, then review §7 and the release
   checklist before exposing it publicly.
