# MC & S CoWorker v3

Server-native AI accounting practice automation. Built on a DigitalOcean droplet in Sydney (SYD1) with FastAPI, PostgreSQL 16 + pgvector, Redis, and Caddy.

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async), Alembic, Anthropic Claude
- **Database:** PostgreSQL 16 with pgvector, pg_trgm, pgcrypto
- **Cache/Queue:** Redis 7
- **Reverse Proxy:** Caddy (auto TLS via Let's Encrypt)
- **Hosting:** DigitalOcean Droplet, SYD1 (4 vCPU / 8 GB)

## Domain

`https://coworker.mcands.com.au`

## Quick Start (Development)

```bash
# Clone
git clone git@github.com:faceless-truth/Coworker_v3.git
cd Coworker_v3

# Install Python deps
uv sync

# Copy and configure environment
cp .env.example .env
# Edit .env with your values

# Run migrations
cd backend && uv run alembic upgrade head

# Start API
uv run uvicorn coworker.api.main:app --reload --port 8001
```

## Deployment

```bash
cd backend
./scripts/deploy.sh
```

## Phase Status

| Phase | Description | Status |
| :--- | :--- | :--- |
| 0 | Droplet provisioning & hardening | ✅ Complete |
| 1 | Server skeleton & database foundation | ✅ Complete |
| 2 | Identity, tenancy & security foundations | ✅ Complete (Stages A, B1, B2, C1 of pre-Phase-3 audit complete; C2 in progress) |
| 3 | External service connectors | ⏭️ Next |
| 4–16 | ... | ⏳ Pending |
