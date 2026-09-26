# Wallet Risk & Portfolio Tracker

Paste an Ethereum address, get a 0-100 risk score based on transaction-graph proximity to
sanctioned and known-malicious addresses, plus a portfolio view and a graph of flagged paths.

> Status: Phase 5 (API). See `CLAUDE.md` for the full build plan.

## Run locally

```bash
cp .env.example .env        # add API keys as later phases need them
docker compose up --build
```

- Frontend: http://localhost:5173 (shows backend health)
- API docs: http://localhost:8000/docs

Score a wallet (the job runs in the background; poll until `done`):

```bash
curl -s -X POST localhost:8000/api/v1/scores -H 'content-type: application/json' \
  -d '{"address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
curl -s localhost:8000/api/v1/scores/<id>
```

Labels must be loaded once first (see Scripts: `ingest_ofac.py`, `ingest_labels.py`).

## Scripts

Run inside the backend container (it reads your root `.env`, including `ETHERSCAN_API_KEY`):

```bash
docker compose run --rm backend python scripts/fetch_transactions.py 0xADDRESS  # all 3 transfer types via cache
docker compose run --rm backend python scripts/cache_stats.py                   # hit rate, calls saved
docker compose run --rm backend python scripts/ingest_ofac.py                   # OFAC SDN -> sanctioned
docker compose run --rm backend python scripts/ingest_labels.py                 # exchange/mixer seed
docker compose run --rm backend python scripts/build_graph.py 0xADDRESS         # BFS graph + flagged addresses
docker compose run --rm backend python scripts/score_address.py 0xADDRESS       # risk score + breakdown
```

Label ingests are idempotent syncs; rerun them anytime. `scripts/build_label_seed.py`
regenerates `backend/config/labels/etherscan_labels.csv` from its pinned source.

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
