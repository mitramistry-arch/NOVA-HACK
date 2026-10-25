# Nova Workspace frontend

A React + TypeScript + Vite interface for the backend workflows that exist today: ingestion, question answering, draft verification, and single-record audit lookup. The UI deliberately does not invent a memory browser, activity feed, usage analytics, account page, or audit list because the API does not expose those capabilities.

## Requirements

- Node.js 20.19+ or 22.12+ (the Vite 6 toolchain supports modern Node releases)
- Python 3.11+ for the local API
- The Python package installed from the repository root with `python -m pip install -e ".[server]"`

## Local development

1. In a terminal at the repository root, configure an isolated development environment and start the API:

   ```bash
   export NOVA_ENV=development
   export NOVA_DB_PATH=./nova-dev.sqlite
   export NOVA_CONTROL_DB_PATH=./nova-dev-control.sqlite
   python -m uvicorn nova.api.main:app --host 127.0.0.1 --port 8000
   ```

2. In a second terminal, enter `frontend/`, install the exact package versions declared by `package.json`, and start Vite:

   ```bash
   npm install
   npm run dev
   ```

3. Open `http://127.0.0.1:5173`. Vite forwards `/api/*` to the loopback-only API target at `http://127.0.0.1:8000`.

For local development only, the Vite Node process can add `X-API-Key` from its own `NOVA_API_KEY` environment variable. The value is not exposed via a `VITE_*` variable, build define, or browser storage. The development proxy rejects non-loopback targets by design. Do not use the Vite development server as a public deployment gateway.

## Hosted deployment and authentication

The backend uses static API-key authentication, not browser sessions. **Do not set `VITE_API_KEY`, put keys in local/session storage, or include deployment API keys in client JavaScript or source maps.** A hosted frontend must be behind a trusted same-origin backend-for-frontend/reverse proxy that authenticates the user with a real session and maps that identity to an authorized tenant-specific backend credential. The proxy must not trust a client-supplied `X-Tenant-ID`; tenant mapping is server-side. Until that deployment layer exists, a browser-facing production deployment is not supported.

Set the public API base URL through the server/reverse-proxy routing, not a secret. Requests use same-origin credentials and `/api` paths; no API key is accepted by frontend code.

## Provider transparency

The repository's default development integration uses `FakeLLM`. It is a deterministic testing double, not a real language model. The UI makes this explicit and does not claim that the public health endpoint identifies a provider. `FakeLLM` was not modified as part of this frontend work.

## Quality commands

```bash
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
```

The Playwright suite includes a response-contract smoke test against a local FastAPI process in `NOVA_ENV=test`, plus UI tests for mobile navigation and verdict rendering. Browser tests and build commands must be run after installing dependencies. Never interpret mocked UI tests as real backend integration tests.
