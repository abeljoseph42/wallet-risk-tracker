# Architecture

Design decisions and their tradeoffs, recorded as they are made.

## Phase 0: Scaffold

- **Health check hits the database.** `/health` runs `SELECT 1` and returns 503 with
  `database: unavailable` if Postgres is unreachable. A liveness-only check would report
  "healthy" while every real request fails; including the DB makes the docker-compose and CI
  smoke test prove the whole stack works.
- **Frontend reaches the API through the Vite dev proxy (`/api` → backend).** The browser only
  talks to one origin in dev, so there's no CORS work to debug locally. CORS is still configured
  from `CORS_ORIGINS` because production (Phase 9) may serve the frontend from another origin.
- **Async SQLAlchemy + asyncpg, async Alembic env.** Every external call (Etherscan, DB) is
  I/O-bound, so one async stack avoids mixing sync DB calls into async request handlers.
- **Unverified external limits are left unset.** `ETHERSCAN_RATE_LIMIT_PER_SEC` has no default;
  it will be filled from Etherscan's current docs in Phase 1 instead of guessed.
- **mypy strict + ruff, strict TypeScript + ESLint** from day one, enforced in CI, so type
  debt never accumulates.
