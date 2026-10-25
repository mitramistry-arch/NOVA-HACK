# Phase 5 implementation and validation status

> **Historical snapshot — not a current status, and not the roadmap's Phase 5.**
> This file predates the ten-phase roadmap and uses the repository's own earlier phase numbering.
> Roadmap **Phase 5** is `EMB-1` (production embeddings and retrieval quality), which is unrelated.
>
> Every number below was true **when it was written**, and three of its claims have since been
> superseded:
> - "492 tests" → the backend suite is now **676 passed, 1 skipped** (skip: opt-in real-PostgreSQL
>   contract test). See `docs/phase-4-report.md`.
> - "ruff ... and `python -m build` are unavailable locally" → both run here; the lint gate was made
>   green in commit `b1f94d1` (`CI-1`) and `python -m build` + `scripts/check_wheel.py` pass.
> - "the environment cannot resolve the npm package registry" → dependencies do install; the real
>   frontend findings were a `vite` dependency-graph typecheck/build failure and 3 of 12 Vitest
>   failures, recorded in `docs/production-readiness-matrix.md` §1.
>
> Current status lives in [`production-readiness-matrix.md`](production-readiness-matrix.md) and the
> per-phase reports. This page is kept unchanged below as the historical record.

**Status at this snapshot: source implementation present; release validation blocked.** This document records the checks run against the integrated repository and does not equate source presence or passing backend tests with a production-ready web release.

## Implemented in source

- React + TypeScript + Vite workspace for ingestion, asking questions, answer verification, and exact-ID audit lookup.
- UI output is based on API responses. There is no fabricated memory browser, audit list, usage dashboard, or provider identity.
- Client-side runtime response decoders reject malformed or incompatible API payloads before rendering.
- Error messages avoid echoing arbitrary server response details. Failed requests preserve the user's draft.
- Browser requests use same-origin `/api` paths; the Vite development proxy accepts only loopback API origins and reads its optional API key from the Node process environment, not browser code.
- Built-asset guard rejects source maps, configured secret values, API-key header strings, and browser storage/cookie persistence.
- Component and Playwright test sources cover primary workflows, error preservation, provider transparency, responsive navigation, and a local FastAPI response-contract smoke test.
- Benchmark fake mode has a separate explicit smoke adapter for valid empty extraction JSON; `nova.core.llm.FakeLLM` itself remains unchanged. The fake benchmark is not model-quality evidence.

## Checks completed in this environment

- `pytest -q -W error::ResourceWarning`: **492 passed**.
- `python -m compileall -q nova benchmark scripts tests`: passed.
- TypeScript compiler `transpileModule` syntax pass on frontend source, test, and configuration `.ts`/`.tsx` files: passed. This is syntax validation only, not semantic type-checking.
- Node runtime smoke of the transpiled API client: valid payload accepted, malformed payload rejected with a contract error, and network failure classified safely.
- FastAPI HTTP smoke: `/healthz` returned the documented liveness payload and `/verify` returned the expected decision contract.
- Anchor fake benchmark smoke: zero system errors. Its expected scores are zero in this mode because fake extraction deliberately emits an empty list; this validates error-free execution only and is **not** a quality benchmark.
- Python wheel and source distribution built through the installed setuptools backend. The wheel-content guard passed, and an extracted source distribution independently passed all **492 tests** with `ResourceWarning` promoted to errors (492 was the suite size at this snapshot; see the banner above). The source distribution manifest includes scripts, frontend source, and the GitHub workflow files read by regression tests; the Python wheel intentionally contains backend Python modules only.
- `git diff --check`: passed.

## Release blockers / unverified checks

The environment cannot resolve the npm package registry. `npm install --package-lock-only` was retried and timed out; there is no verified `frontend/package-lock.json`. Do not create a hand-written or guessed lockfile. Therefore the following have **not** been verified:

- `npm ci` with a committed lockfile.
- `npm run typecheck` (the global `tsc` stops because the project dependencies and their type definitions are not installed).
- `npm run lint`, Vitest component tests, the Vite production build, the built-asset check against a real bundle, and Playwright browser tests.
- A visual desktop/mobile browser review and accessibility review using a running built frontend.
- Ruff lint/format and `python -m build` command itself (the packages are unavailable locally). Wheel and sdist were built directly with the installed setuptools backend instead. That direct build emits a non-fatal setuptools deprecation warning because `project.license` still uses the legacy TOML table form; the metadata should be migrated in a separately validated build-toolchain change.
- Real PostgreSQL integration, target-deployment BFF/session/CSRF behavior, and operational deployment checks.

## Required before release

1. In an environment with a working npm registry, generate `frontend/package-lock.json` with the declared exact dependency versions and commit it.
2. Run `npm ci`, `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`, and `npm run test:e2e`; fix all failures rather than relaxing checks.
3. Inspect the actual production bundle with `python scripts/check_frontend_assets.py frontend/dist` and review the UI in a browser at desktop and mobile breakpoints.
4. Run Ruff, the standard `python -m build`, clean runtime-only installation, full supported Python-version CI, and real PostgreSQL/deployment-specific integration tests.
5. Deploy the UI only behind a trusted same-origin backend-for-frontend that authenticates a real session and maps the user server-side to an authorized tenant credential. Never expose the backend static API key to browser JavaScript.
