# CoWorker Frontend

React 19 + Vite + TypeScript + TanStack Query + Tailwind 4.

## Phase 10-1: scaffold

This directory was empty until Phase 10-1. It now contains a
minimal `/health` page that verifies the Vite dev-server proxies
correctly to the FastAPI backend on port 8001.

```bash
# From this directory
npm install       # or: pnpm install / bun install
npm run dev       # serves on http://localhost:5173
```

While the dev server runs, requests to `/health`, `/auth`,
`/approval`, `/mail`, and `/webhooks` are proxied to the
backend (default `http://127.0.0.1:8001`; override with
`VITE_API_TARGET`). The session cookie flows through the proxy
unchanged so an authenticated session in the backend works in
the frontend.

## Next sub-phases

- 10-2: OAuth login wrapper (`/auth/microsoft/start` + callback)
  and an unauthenticated landing redirect.
- 10-3: approval queue list page (`GET /approval/pending`).
- 10-4: approval item detail with edit (PUT) and approve/reject
  (POST) buttons.
