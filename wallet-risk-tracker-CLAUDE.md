# Wallet Risk & Portfolio Tracker: Build Instructions

> Save this as `CLAUDE.md` in the repo root (Claude Code) or paste it into your Claude project instructions.
> Work **one phase at a time**. At the end of each phase: run the tests, summarize what changed, list any open questions, and **wait for my go-ahead** before starting the next phase.

---

## 1. Project overview

Build a web app where a user pastes a public **Ethereum wallet address** and gets:

1. A **risk score (0-100)** with an explainable breakdown, based on how close the wallet's transaction network is to sanctioned/malicious addresses.
2. A **portfolio view** of the wallet's holdings.
3. A **graph visualization** of the flagged paths connecting the wallet to risky addresses.

This is a resume project for new grad SWE recruiting (targets include Coinbase and similar companies), so **depth, measurability, and defensibility in interviews matter more than feature count.** Scope is **on-chain risk analysis only**. Do not add stock/brokerage portfolio tracking.

### Resume bullets this project must make true

```
Wallet Risk & Portfolio Tracker | Python, FastAPI, React, NetworkX, PostgreSQL, Etherscan, Docker, AWS
* Built a risk-scoring engine for Ethereum wallets that ingests transaction history from the Etherscan API
  and traverses the transaction graph in NetworkX to measure proximity to OFAC-sanctioned and known-malicious
  addresses; achieved [X]% recall / [Y]% precision on a labeled set of [N] addresses.
* Designed hop-weighted scoring that discounts distant and high-volume exchange counterparties, cutting false
  positives by [X]%; added a PostgreSQL caching layer that reduced Etherscan calls by [X]% and kept the app
  within rate limits.
```

**Hard rule:** every number that ends up in those bullets must be **measured by code in this repo** (see the evaluation and metrics phases). Never invent, estimate, or "fill in" metrics. If a number isn't measured yet, leave the placeholder.

---

## 2. Tech stack

**Backend:** Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2.x + Alembic, PostgreSQL 16, NetworkX, httpx (async), pydantic v2 + pydantic-settings, pytest + pytest-asyncio, ruff, mypy (or pyright).
**Frontend:** React + Vite + TypeScript, TanStack Query, a graph lib (`react-force-graph-2d` or Cytoscape.js), Tailwind (optional).
**Infra:** Docker + docker-compose for local dev; AWS for deployment at the end (decide the exact service with me in Phase 9).

---

## 3. Target repo structure

```
wallet-risk-tracker/
├── CLAUDE.md
├── README.md
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py            # settings + scoring params loader
│   │   ├── db/                  # models, session, repositories
│   │   ├── clients/             # etherscan.py, price.py, balances.py
│   │   ├── services/
│   │   │   ├── cache.py         # cache-aware fetch layer + metrics
│   │   │   ├── labels.py        # OFAC/exchange label ingestion
│   │   │   ├── graph.py         # graph builder
│   │   │   ├── scoring.py       # hop-weighted scoring
│   │   │   └── portfolio.py
│   │   ├── api/                 # routers + schemas
│   │   └── core/                # rate limiter, logging, errors
│   ├── scripts/                 # ingest_ofac.py, build_eval_set.py, run_eval.py
│   ├── config/scoring.yaml
│   └── tests/
├── frontend/
│   ├── Dockerfile
│   └── src/
├── data/                        # gitignored raw data; keep small fixtures in tests/fixtures
└── docs/
    ├── ARCHITECTURE.md
    ├── SCORING.md
    └── EVALUATION.md
```

---

## 4. Working agreements

- **Verify external facts before coding against them.** Before writing the Etherscan client, check Etherscan's *current* docs for the API version/base URL, endpoint names, auth, and free-tier rate limits. Don't assume them from memory. Put the confirmed limits in config, not hard-coded.
- **Small, reviewable commits.** One logical change per commit, with clear messages.
- **Tests come with the code.** Every service gets unit tests. Use recorded fixtures for Etherscan responses; never hit the live API in tests.
- **Type hints everywhere;** `ruff` and the type checker must pass.
- **No secrets in git.** API keys via `.env`; keep `.env.example` current.
- **Validate input.** Normalize and validate Ethereum addresses (format + EIP-55 checksum handling; store lowercase canonical form).
- **Async I/O** for all external calls. Handle rate limits, timeouts, retries with backoff, and Etherscan's "no transactions found" and error payloads gracefully.
- **Explain tradeoffs.** When you make a design decision (data structure, threshold, library), write one or two sentences in `docs/ARCHITECTURE.md` so I can defend it in an interview.
- **Ask, don't guess,** when a requirement is ambiguous or a choice is expensive to reverse.

---

## 5. Data model (initial)

| Table | Key columns | Purpose |
|---|---|---|
| `transactions` | `hash` (pk), `from_addr`, `to_addr`, `value_wei`, `block_number`, `timestamp`, `is_error`, `source_endpoint` | Cached normal (and later token/internal) transfers |
| `fetch_log` | `address`, `endpoint`, `last_fetched_at`, `last_block`, `complete` (bool) | Tracks what has been fetched so we skip or do incremental refreshes |
| `address_labels` | `address` (pk), `label_type` (`sanctioned` / `malicious` / `exchange` / `mixer`), `source`, `severity`, `added_at` | Ground truth and exchange labels |
| `api_metrics` | `id`, `ts`, `endpoint`, `cache_hit` (bool), `latency_ms` | Raw data for the cache-hit-rate / call-reduction numbers |
| `score_runs` (optional) | `id`, `address`, `score`, `params_hash`, `breakdown_json`, `created_at` | Reproducibility and demo history |

Use Alembic migrations from the start. Index `from_addr`, `to_addr`, and `(address, endpoint)` on `fetch_log`.

---

## 6. Phases

### Phase 0: Scaffold
- Create the repo structure above, `docker-compose.yml` (backend, frontend, postgres), `.env.example`, ruff/mypy/pytest config, and a CI workflow (GitHub Actions: lint, type check, tests).
- Backend: `/health` endpoint. Frontend: a page that calls `/health` and shows the result.
- **Done when:** `docker compose up` brings up all three services, the frontend displays a healthy backend, and CI is green.

### Phase 1: Etherscan client + cache layer
- Build an async Etherscan client with: a token-bucket rate limiter (limit from config), retry with exponential backoff, pagination for large histories, and typed response models.
- Build `services/cache.py`: given `(address, endpoint)`, return cached transactions from Postgres if fresh per `fetch_log`; otherwise fetch, upsert, update `fetch_log`. Support incremental refresh using `last_block`.
- Record every call to `api_metrics` (cache hit vs. miss, latency).
- **Done when:** tests prove a second request for the same address makes zero Etherscan calls, rate limiter tests pass with a fake clock, and a `scripts/` command prints cache hit rate and total API calls saved.

### Phase 2: Label ingestion (ground truth)
- Write `scripts/ingest_ofac.py` to load OFAC-sanctioned Ethereum addresses into `address_labels` (`label_type=sanctioned`). Prefer parsing the **official OFAC SDN list**; if you use a community-maintained parsed list, document the source and pull date.
- Add a mechanism to load exchange labels (`exchange`) and other malicious labels from a CSV/JSON seed file, with `source` recorded for each.
- **Done when:** the script is idempotent, the counts are logged, and there are tests using a small fixture.

### Phase 3: Graph builder
- Given a start address and `max_hops`, build a directed `networkx.DiGraph` by BFS using the cache layer. Nodes = addresses; edges aggregate `tx_count`, `total_value_wei`, and `last_seen`.
- Guardrails (all configurable): max neighbors expanded per node, max total nodes, skip zero-value/failed txs, and do **not expand through** labeled exchanges or nodes over a degree threshold (still record them as nodes).
- **Done when:** tests on small synthetic graphs pass, and a run on a real address respects the caps and never blows the rate limit. Document the BFS/expansion strategy and its complexity in `ARCHITECTURE.md`.

### Phase 4: Hop-weighted scoring
- Implement `services/scoring.py` per the spec in section 7. Return the total score **and a breakdown** (each flagged address, its label, hop distance, shortest path, contribution).
- Load all parameters from `config/scoring.yaml`; include the params hash in the output.
- **Done when:** unit tests cover directly-sanctioned wallets, 1/2/3-hop cases, exchange discounting, and a wallet with no flagged neighbors (score 0). `docs/SCORING.md` explains the formula.

### Phase 5: API
- `POST /api/v1/score` (or `GET /api/v1/wallets/{address}/risk`): returns score, breakdown, and graph data (nodes/edges for the flagged subgraph only, not the whole graph).
- Proper errors for invalid addresses, upstream rate-limit or outage, and timeouts. Consider running long jobs as background tasks with a polling endpoint if latency is high.
- OpenAPI docs should be clean and accurate.
- **Done when:** integration tests pass with mocked Etherscan.

### Phase 6: Evaluation harness (do this before tuning)
- `scripts/build_eval_set.py` creates a labeled evaluation set and stores it as a versioned file in `data/eval/`.
  - **Positives:** wallets within 1-2 hops of sanctioned addresses (e.g., wallets that interacted with sanctioned mixer addresses).
  - **Negatives:** randomly sampled active wallets **and** known exchange hot/cold wallets (these stress false positives).
  - **Leakage rule:** exclude the sanctioned addresses themselves from the eval set. Detecting a wallet that is literally on the list is trivial and would inflate results.
- `scripts/run_eval.py` computes precision, recall, F1, a PR curve, and the false-positive rate on exchange wallets, and can compare **hop-weighting on vs. off** and different parameter sets.
- Write results to `docs/EVALUATION.md` with the eval set size, date, and params hash.
- **Done when:** one command reproduces every number, and the output clearly reports `N`, precision, recall, and the false-positive reduction from hop weighting/exchange discounting.

### Phase 7: Portfolio holdings
- Etherscan's free tier may not give clean ERC-20 balances. Propose a second data source (e.g., Alchemy or another provider) and a price source (e.g., CoinGecko), **check their current free-tier limits and terms, and ask me before adding a new provider.**
- Return ETH + top token holdings with USD values; cache prices and balances with sensible TTLs.
- **Done when:** the holdings endpoint works for a few known wallets and degrades gracefully if a provider is down.

### Phase 8: Frontend
- Address input with client-side validation, loading and error states, a risk score gauge with a plain-language explanation, a breakdown table (flagged address, label, hops, contribution), a holdings table, and the force-directed graph of flagged paths (color by label type, size by value).
- Make it responsive and accessible (keyboard focus, contrast). Keep the design clean and professional.
- **Done when:** a demo run on a known risky wallet and a known clean wallet tells a clear story in under 30 seconds.

### Phase 9: Deploy
- Production Dockerfiles, environment config, and a deploy plan for AWS. Propose 1-2 options with cost and complexity tradeoffs (e.g., EC2 + docker-compose vs. App Runner/ECS + RDS) and **ask me to pick.**
- Add HTTPS, CORS config, basic request rate limiting on our own API, and structured logging.
- **Done when:** the app is live at a public URL and the README links to it. (Only list AWS on the resume once this is true.)

### Phase 10: Polish and resume prep
- README with architecture diagram, setup instructions, screenshots or a GIF, and a "Design decisions" section.
- Pull the measured numbers from `docs/EVALUATION.md` and the metrics script, and produce the final resume bullets with real values.
- Write a list of 10-15 likely interview questions about this project (graph traversal choice, false positives, rate limiting, caching invalidation, scaling to millions of wallets) with concise answers grounded in what we actually built.

---

## 7. Scoring spec (starting point, all values tunable)

- For each flagged address `f` reachable from the target within `max_hops` (default 3), take the **shortest path** distance `d`.
- Contribution: `c_f = severity(label) × hop_decay^(d-1) × flow_factor`
  - `severity`: sanctioned = 1.0, malicious = 0.8, mixer = 0.7 (configurable).
  - `hop_decay`: start at ~0.5 (so 1 hop = full weight, 2 hops = half, 3 hops = a quarter).
  - `flow_factor`: bounded 0-1 measure of how much value moved along the path relative to the wallet's total volume, so trivial dust transfers count for less. Document the exact definition you choose.
- **Exchange handling:** when a path passes through a labeled exchange or a node above the degree threshold, multiply that path's contribution by `exchange_discount` (start ~0.2) or cut the path entirely. Make this a switch so the evaluation can measure with vs. without it.
- **Aggregation:** `score = 100 × (1 - Π(1 - c_f))` over all flagged addresses, so multiple weak signals add up but the score stays bounded at 100.
- Output: the score, a bucket (Low / Medium / High / Severe) with configurable thresholds, and the full breakdown.

Treat these as defaults to be **tuned using the Phase 6 evaluation harness**, not final answers.

---

## 8. Environment variables (`.env.example`)

```
DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/wallet_risk
ETHERSCAN_API_KEY=
ETHERSCAN_RATE_LIMIT_PER_SEC=        # confirm from current Etherscan docs
BALANCES_API_KEY=                    # only if Phase 7 adds a provider
PRICE_API_KEY=                       # optional
CACHE_TTL_SECONDS=
LOG_LEVEL=INFO
CORS_ORIGINS=http://localhost:5173
```

---

## 9. Definition of done (whole project)

- [ ] `docker compose up` runs everything locally from a clean clone using only `.env.example` + API keys
- [ ] CI passes (lint, types, tests)
- [ ] Evaluation and metrics are reproducible with single commands, and results are in `docs/EVALUATION.md`
- [ ] Deployed at a public URL
- [ ] README, architecture doc, and scoring doc are complete
- [ ] Final resume bullets contain only measured numbers

---

## 10. First message to start with

> Read `CLAUDE.md` fully. Then start **Phase 0**. Before writing code, give me a short plan (files you'll create, decisions you'll make, anything you need from me such as API keys). Then implement Phase 0, run the checks, and stop for my review.
