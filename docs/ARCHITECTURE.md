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
