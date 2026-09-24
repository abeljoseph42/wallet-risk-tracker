# Wallet Risk & Portfolio Tracker

Paste an Ethereum address, get a 0-100 risk score based on transaction-graph proximity to
sanctioned and known-malicious addresses, plus a portfolio view and a graph of flagged paths.

> Status: Phase 1 (Etherscan client + cache). See `CLAUDE.md` for the full build plan.

## Run locally

```bash
cp .env.example .env        # add API keys as later phases need them
docker compose up --build
```

- Frontend: http://localhost:5173 (shows backend health)
- Backend: http://localhost:8000/health, API docs at http://localhost:8000/docs

## Scripts

Run inside the backend container (it reads your root `.env`, including `ETHERSCAN_API_KEY`):

```bash
docker compose run --rm backend python scripts/fetch_transactions.py 0xADDRESS  # fetch via cache
docker compose run --rm backend python scripts/cache_stats.py                   # hit rate, calls saved
```

## Develop without Docker

Backend (Python 3.12). Tests need the compose Postgres, published on host port 5433
(`docker compose up -d db`); they use a separate `wallet_risk_test` database:

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest
.venv/bin/uvicorn app.main:app --reload
```

Frontend (Node 22):

```bash
cd frontend
npm ci
npm run lint && npm run typecheck && npm test
npm run dev
```

## Docs

- [Architecture decisions](docs/ARCHITECTURE.md)
- [Scoring](docs/SCORING.md)
- [Evaluation](docs/EVALUATION.md)
