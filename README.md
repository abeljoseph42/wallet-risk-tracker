# Wallet Risk & Portfolio Tracker

Paste an Ethereum address, get a 0-100 risk score based on transaction-graph proximity to
sanctioned and known-malicious addresses, plus a portfolio view and a graph of flagged paths.

> Status: Phase 0 (scaffold). See `CLAUDE.md` for the full build plan.

## Run locally

```bash
cp .env.example .env        # add API keys as later phases need them
docker compose up --build
```

- Frontend: http://localhost:5173 (shows backend health)
- Backend: http://localhost:8000/health, API docs at http://localhost:8000/docs

## Develop without Docker

Backend (Python 3.12, needs a Postgres at `DATABASE_URL`):

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
