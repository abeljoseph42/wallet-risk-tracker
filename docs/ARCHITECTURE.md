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

## Phase 1: Etherscan client + cache layer

Etherscan facts verified against docs.etherscan.io on 2026-09-24: V2 base URL
`https://api.etherscan.io/v2/api` with `chainid=1`; free tier 3 calls/s and 100k calls/day;
`txlist` returns at most 1,000 records per page and 10,000 per (address, block range) query.

- **Token bucket with a burst of 1.** Calls are spaced evenly at 1/rate seconds. A bucket
  that allowed a burst equal to the rate let 4 calls land inside one second during a live
  78k-transaction fetch, and Etherscan throttled at least 14 of them. With a burst of 1, the
  next live fetch was throttled 0 times. The clock and sleep are injectable, so tests run
  on a fake clock with no real waiting.
- **Retry only what can succeed on retry.** Transport errors, HTTP 5xx, HTTP 429, and
  Etherscan's HTTP-200 "Max calls per sec rate limit reached" payload are retried with
  exponential backoff. Other 4xx errors and API errors like an invalid key fail
  immediately. "No transactions found" is an empty result, not an error.
- **Pagination past the 10,000-record window.** Page through the window with `page`/`offset`,
  then restart the query at the *last block seen* (not last + 1) and de-duplicate by hash.
  A window can end in the middle of a block, and restarting at last + 1 would silently drop
  that block's remaining transactions, which is common for busy addresses.
- **Freshness via `fetch_log` + TTL (default 1h), incremental refresh.** A lookup inside the
  TTL is served entirely from Postgres (zero API calls). A stale lookup fetches only from
  `fetch_log.last_block` onward and upserts with `ON CONFLICT DO NOTHING`, so history is
  never refetched. Transaction history only grows, so the cache never has to invalidate
  what it already holds. The TTL only controls how quickly new transactions show up.
- **Postgres upserts, tested against real Postgres.** Idempotent upserts make concurrent or
  repeated refreshes safe. The tests use a real Postgres (a service container in CI)
  instead of SQLite or mocks, because `ON CONFLICT` and `NUMERIC` behave differently elsewhere.
- **`value_wei` is `NUMERIC(78,0)`, read as `Decimal`.** A uint256 needs 78 digits; a float
  would silently round large amounts.
- **Metrics are measured, with a conservative "calls saved".** Each lookup writes one
  `api_metrics` row with `cache_hit`, `latency_ms`, `upstream_calls` (real HTTP requests,
  retries included), and `calls_avoided`. `calls_avoided` counts the txlist pages a
  refetch would have needed, `ceil(cached_txs / 1000)`. This is a lower bound because it
  ignores retries. The client reports request counts per fetch, not through a shared
  counter, so concurrent lookups don't mix up each other's numbers.
- **Addresses are stored lowercase.** Format is validated. EIP-55 checksum verification
  is still open, because it needs a Keccak-256 dependency.

## Phase 2: Label ingestion

Ground truth as of the first ingest (2026-09-24): **124** OFAC-sanctioned Ethereum
addresses (SDN list published 2026-09-23), **309** exchange wallets, and **32** mixer
contracts. Any address-level overlap between these sets: none.

- **Official OFAC source, legacy `SDN.XML` format.** Downloaded from the Sanctions List
  Service (`sanctionslistservice.ofac.treas.gov/.../exports/SDN.XML`), which 302-redirects
  to a short-lived signed S3 URL. `SDN.XML` lists each address as a flat `<id>` with
  `idType` "Digital Currency Address - ETH". `SDN_ADVANCED.XML` would require resolving
  feature-type IDs to get the same data. The parser matches elements with a namespace
  wildcard, so a namespace URL change can't break it silently.
- **Every Ethereum-format address, not only `- ETH`.** OFAC lists some Ethereum addresses
  only under a token (USDT, USDC) or another EVM chain (ARB, BSC, ETC). An EVM address is
  the same private key on every EVM chain, so any `Digital Currency Address - *` value
  that is a 0x 20-byte address counts. That adds 4 addresses to the 120 tagged ETH.
- **Tornado Cash is no longer on the SDN list.** Treasury delisted it in 2025; today's list
  has no Tornado contract addresses. Its pools are therefore labeled `mixer` from the seed,
  not `sanctioned`. This matters for Phase 6: positives near Tornado measure proximity to
  a mixer, not to a currently sanctioned address.
- **Exchange and mixer labels come from Etherscan's name tags,** taken from the
  `brianleect/etherscan-labels` dump (MIT, pinned to commit `923aba7`, 2023-10-01), and
  curated by tested rules in `services/label_seeds.py`. Exchange: operational wallets
  only (numbered hot/cold wallets, deposit funders, old addresses). Mixer: Tornado deposit
  pools plus Mixer, Proxy, and Router contracts. Token contracts, deployers, fee
  addresses, DEX routers, and Tornado governance/vesting contracts are excluded, because
  they aren't where user funds move through. The generated CSV is committed, so the
  ground truth doesn't change unless someone reruns `build_label_seed.py`. Tradeoff: the
  dump is from 2023, so exchange wallets created since then are missing.
- **Composite key `(address, label_type)`.** An address can be both a mixer and
  sanctioned (Tornado pools were, until 2025). A single-column key would force picking one.
- **`severity` is an optional override.** `NULL` means "use `scoring.yaml`'s default for the
  label type", so tuning severities in Phase 6 is a config change, not a data migration.
- **Ingest syncs a source instead of appending to it.** Each run makes the rows for that
  `source` exactly match the input: new rows are inserted, changed rows updated (only
  when their content changed, so a rerun reports 0), and rows the source dropped are
  deleted. That's how an OFAC delisting takes effect. An empty parse is refused, so an
  upstream format change can't wipe the table.

## Phase 3: Graph builder

### Data: internal and token transfers

- **All three transfer types are cached.** Etherscan's `txlist`, `txlistinternal` and
  `tokentx` (by address, free tier, 1,000 records per page since July 2026). Mixer
  withdrawals reach the user as *internal* transfers, because the pool contract sends the
  ETH. A graph built on normal transactions alone would miss exactly the wallets we most
  want to flag.
- **One table, keyed by `(source_endpoint, hash, sub_key)`.** One transaction hash can hold
  several internal calls and token transfers. `sub_key` is the `traceId` for internal
  transfers. `tokentx` has no log index, so a token transfer's key is
  token/from/to/value plus an occurrence number for identical transfers in the same
  transaction. Occurrence numbers restart with each 10,000-record window, and because a
  window always re-reads its boundary block from the start, the numbering stays consistent.
  The migration is hand-written so renaming `value_wei` to `value_raw` (base units of the
  asset) keeps the cached rows.
- **Record caps with resumable reads.** A lookup can cap how many records it reads. A capped
  read is stored with `complete=False`. It's served to later lookups whose cap it already
  meets, and resumed from `last_block` by lookups that need more.
- **80% of the plan rate limit.** Requests leave evenly spaced, but network jitter bunches
  them on arrival. At 100%, a 49-call build was throttled 5 times. At 80%, builds of 22
  and 107 calls were throttled 0 times.

### Traversal: bounded breadth-first search

Starting from the target, BFS expands one hop level at a time, up to `max_hops`
(default 3). Distance ignores edge direction: receiving from a mixer counts as much as
sending to one. Edges stay directed for display and scoring.

- **Why BFS.** Scoring needs each flagged address's *shortest* hop distance
  (`hop_decay^(d-1)`), and BFS finds it directly. Expanding level by level also means
  that when the budget runs out, it's the farthest (least relevant) addresses that get
  left out.
- **Labeled addresses are endpoints.** Exchanges, mixers and sanctioned addresses are
  recorded but never expanded. Expanding a mixer pool or an exchange would pull in
  thousands of unrelated users and turn every wallet into a 2-hop neighbor of everything.
- **Hubs are recorded, not expanded.** An unlabeled address that fills its record cap
  (`max_records_per_node`, 2,000 per endpoint) or has more than `degree_threshold` (500)
  counterparties is a hub: DEX routers, token contracts, and exchange wallets missing
  from our 2023 labels. A hub keeps its edges to nodes already in the graph but adds no
  new ones, so no path runs *through* it to a newly found address. Neighbors fetch
  normal transfers first, and if that already shows a hub, the internal and token
  lookups are skipped, saving two-thirds of the calls on each hub.
- **Neighbor selection.** Each expansion queues at most `max_neighbors_per_node` (10)
  unlabeled counterparties, ranked by transfer count, then ETH value. Labeled
  counterparties are always recorded, since recording them needs no fetch. So the cap
  can't hide a flagged address that sits one hop from any expanded node. Unlabeled
  counterparties that won't be expanded aren't added as nodes, just counted
  (`skipped_neighbors`). Recording them let vitalik.eth's thousands of direct
  counterparties fill the node limit at hop 1 and starve hops 2 and 3.
- **Filters.** Failed transfers and zero-value transfers are skipped. Zero-value token
  transfers are how "address poisoning" plants a lookalike address in a wallet's history.
  Self-transfers are skipped too. A transfer seen from both of its ends is counted once.
- **Edges** aggregate `tx_count`, `total_value_wei` (ETH only, normal + internal),
  `token_transfer_count` and `last_seen`. Token amounts aren't summed into value, because
  adding raw units of different tokens (or tokens to ETH) is meaningless without prices.

**Complexity.** Let E = `max_expanded_nodes` (100), K = `max_neighbors_per_node` (10),
R = `max_records_per_node` (2,000), R_t = `max_records_target` (20,000), and P = 1,000
records per page.
- API calls on a cold cache: at most 3·⌈R_t/P⌉ + (E−1)·3·⌈R/P⌉ = 60 + 594 = 654
  (plus retries), about 4.5 minutes at 2.4 calls/s. A hub costs only ⌈R/P⌉ = 2.
- Work: aggregation is linear in the rows read, at most 3·R_t + 3·E·R ≈ 660k. Graph
  updates are O(V + E_edges).
- Size: V ≤ 1 + E·K unlabeled nodes plus labeled ones. `max_nodes` (2,000) is a
  safety cap.

Measured on live data (2026-09-24):

| Target | Nodes | Expanded | Hubs | API calls | Rate-limit retries | Time |
|---|---|---|---|---|---|---|
| Tornado 1 ETH depositor A (cold) | 26 | 15 | 1 | 49 | 5 (before 80% headroom) | 19.6 s |
| Tornado 1 ETH depositor B (partly cached) | 29 | 19 | 3 | 22 | 0 | 10.9 s |
| Depositor A again (cached) | 26 | 15 | 1 | 0 | 0 | 0.2 s |
| vitalik.eth (target history cached) | 273 | 41 | 25 | 107 | 0 | 66.3 s |

Both depositors show three Tornado pools at hop 1. vitalik.eth, a well-known wallet that
isn't illicit, also has Tornado contracts at hop 1. That's the false-positive case the
Phase 4 `flow_factor` and the Phase 6 evaluation have to handle.

## Phase 4: Scoring

The formula, parameters and worked examples are in [SCORING.md](SCORING.md). Decisions:

- **Flow = saturating share of the target's volume, measured at the path's bottleneck.**
  Absolute amounts would score a whale's small mixer use like a small wallet's entire
  balance. A raw share would under-score a wallet that moved 20% of its funds through a
  mixer. Live effect: vitalik.eth scores 6.32 despite three Tornado contracts at hop 1,
  while two ordinary Tornado depositors score about 74.
- **No discount for incoming-only links.** Mixer withdrawals are incoming-only, so a
  direction discount would weaken the main signal. Unsolicited dust and spam tokens are
  handled by value instead: dust gets a tiny share, and unpriced tokens are worth 0.
- **Stablecoins at $1 with a pinned ETH price.** Tornado has USDT, USDC and DAI pools, so
  ETH-only value would miss them. A live price feed would make scores (and evaluation
  results) change from day to day; the pinned rate keeps them reproducible.
- **Strongest path wins.** Scoring the best of all shortest paths and shortest
  exchange-free paths means exchange handling only lowers scores when *every* short route
  goes through an exchange or hub.
- **The params hash is computed from the validated model, not the YAML text.** So
  evaluation variants built in code, like exchange handling off, get distinct hashes.
  Comments and key order in the YAML don't change it.
- **Node volume is recorded during graph building.** The flow denominator must include
  transfers to counterparties that were never added to the graph; otherwise shares would
  be inflated.

## Phase 5: API

| Method | Path | Returns |
|---|---|---|
| `POST` | `/api/v1/scores` `{"address": ...}` | 202 with a job (`pending`); 200 if a recent finished result is reused; 422 for a bad address or checksum; 503 if no Etherscan key |
| `GET` | `/api/v1/scores/{id}` | The job: status, then `result` (score, bucket, breakdown, flagged subgraph, traversal stats) or `error` (code + message); 404 if unknown |
| `GET` | `/api/v1/wallets/{address}/scores` | That wallet's runs, newest first |
| `GET` | `/api/v1/health` | Liveness + database check |

Interactive docs: `http://localhost:8000/docs`.

- **Background job plus polling, not a blocking request.** A cold score makes up to
  hundreds of rate-limited Etherscan calls. Live: 23.6 s and 51 calls for a Tornado
  depositor. Load balancers commonly cut idle requests at about 60 s, and a job lets the
  UI show progress. `POST` returns 202 with a `Location` header, and the client polls.
- **Jobs are asyncio tasks inside the API process, with state in Postgres (`score_runs`).**
  No queue infrastructure for a single-instance demo. Tradeoffs: a restart loses
  in-flight jobs, so startup marks them `failed` with code `interrupted` and clients can
  resubmit. Jobs also can't be spread across processes. The next step would be Redis
  plus an `arq` worker, and the `ScoreJobs` interface would stay the same.
- **One Etherscan client per process, at most 2 concurrent jobs.** All jobs share one
  rate limiter, so running more at once would only make them wait on each other. The
  rest queue as `pending`.
- **Reuse and dedupe.** A submit for the same address and params hash returns a finished
  run from the last hour (`SCORE_REUSE_SECONDS`) or the job already in progress. An
  in-process lock makes the check-then-insert atomic, so two identical requests arriving
  together start one job.
- **Failures are data, not 500s.** A job that fails ends `failed` with an error code:
  `upstream_rate_limited` (still throttled after retries), `upstream_unavailable`
  (transport errors or 5xx), `upstream_error`, `timeout` (`SCORE_JOB_TIMEOUT_SECONDS`,
  default 300), `interrupted` or `internal`. The client from Phase 1 already classifies
  the Etherscan errors.
- **Only the flagged subgraph is returned,** meaning the nodes and edges on the
  breakdown's paths, not the whole traversal (which can be hundreds of nodes). Wei
  amounts are decimal strings, because they exceed JavaScript's 2^53 safe-integer range.
- **Every run is stored** with its params hash, so any past score can be traced to the
  exact parameters that produced it.
- **EIP-55 checksum validation** for mixed-case input uses pycryptodome's Keccak-256,
  tested against the spec's vectors. All-lowercase and all-uppercase input is accepted,
  as the spec intends.
- **Everything the frontend calls lives under `/api/v1`,** and the dev proxy passes
  paths through unchanged. Phase 0's proxy stripped `/api`, which broke the versioned
  routes.

The integration tests (`tests/test_api_scores.py`) run the real app against Postgres
and a mocked Etherscan. They cover dedupe of simultaneous submits, result reuse with
zero upstream calls, invalid addresses and checksums, persistent rate limiting,
outages, timeouts, a missing API key, history, restart recovery, and the OpenAPI
contract.
