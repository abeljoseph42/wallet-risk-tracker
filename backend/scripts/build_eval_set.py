"""Build the labeled evaluation set (see docs/EVALUATION.md for the design).

- Pins a snapshot block; everything is judged on history up to it.
- Splits flagged labels into scoring and held-out halves (fixed seed, by SDN entity).
- Positives: EOAs that transacted directly with a held-out address before the snapshot,
  picked round-robin across held-out addresses so no single one dominates.
- Negatives: senders of random transactions from the year before the snapshot, minus
  any that touched a held-out address; plus labeled exchange wallets.
- Leakage rule: no labeled address is ever a positive or a random negative.

Usage: docker compose run --rm backend python scripts/build_eval_set.py \
           [--version NAME --seed N --exclude OTHER_VERSION ...]
Writes data/eval/eval_set_<version>.json. --exclude keeps a test set disjoint from the
wallets of earlier (e.g. tuning) sets. Rerunning with the same labels and seed
reproduces the same set, served mostly from cache.
"""

import argparse
import asyncio
import datetime
import random
from collections import Counter
from pathlib import Path

import httpx
from sqlalchemy import select

from app.clients.etherscan import EtherscanClient, client_from_settings
from app.config import get_settings, load_scoring_params
from app.db.models import AddressLabel, Transaction
from app.db.session import get_engine, get_sessionmaker
from app.services.cache import Snapshot, TransactionCache
from app.services.evaluation import EvalSet, Example, FlaggedLabel, Group, split_labels

EVAL_DIR = Path(__file__).resolve().parents[2] / "data" / "eval"
DEFAULT_SEED = 20260926
TARGETS: dict[Group, int] = {
    "pos_sanctioned": 75,
    "pos_mixer": 75,
    "neg_random": 100,
    "neg_exchange": 50,
}
# Blocks behind the chain head for the snapshot, so it can't be reorganized away.
SNAPSHOT_LAG = 64
# About one year of 12-second blocks.
RANDOM_BLOCK_SPAN = 2_630_000
CANDIDATE_RECORDS = 2_000


def _counterparties(address: str, rows: list[Transaction], snapshot: Snapshot) -> set[str]:
    out = set()
    for row in rows:
        if row.block_number > snapshot.block or row.is_error or row.value_raw == 0:
            continue
        other = row.to_addr if row.from_addr == address else row.from_addr
        if other != address:
            out.add(other)
    return out


async def _history_counterparties(
    cache: TransactionCache, address: str, snapshot: Snapshot, max_records: int
) -> set[str]:
    results = await cache.lookup_all(address, max_records=max_records, snapshot=snapshot)
    rows = [row for result in results.values() for row in result.transactions]
    return _counterparties(address, rows, snapshot)


async def _pick_positives(
    cache: TransactionCache,
    client: EtherscanClient,
    heldout: list[str],
    target: int,
    used: set[str],
    snapshot: Snapshot,
    rng: random.Random,
    group: Group,
) -> list[Example]:
    queues: dict[str, list[str]] = {}
    for address in heldout:
        candidates = sorted(
            await _history_counterparties(cache, address, snapshot, CANDIDATE_RECORDS) - used
        )
        rng.shuffle(candidates)
        if candidates:
            queues[address] = candidates
    print(f"  {group}: {len(queues)}/{len(heldout)} held-out addresses have counterparties")

    picked: list[Example] = []
    while len(picked) < target and queues:
        for source in list(queues):
            if len(picked) >= target:
                break
            queue = queues[source]
            while queue:
                candidate = queue.pop()
                if candidate in used:
                    continue
                if await client.is_contract(candidate):
                    continue
                picked.append(Example(candidate, 1, group, source))
                used.add(candidate)
                break
            if not queue:
                del queues[source]
    return picked


async def main(version: str, seed: int, exclude: list[str]) -> None:
    settings = get_settings()
    params = load_scoring_params()
    sessions = get_sessionmaker()
    async with sessions() as session:
        rows = list(await session.scalars(select(AddressLabel)))
    flagged = [
        FlaggedLabel(r.address, r.label_type, r.name)
        for r in rows
        if r.label_type in ("sanctioned", "mixer", "malicious")
    ]
    exchanges = sorted({r.address for r in rows if r.label_type == "exchange"})
    labeled = {r.address for r in rows}
    rng = random.Random(seed)
    excluded = {
        e.address
        for other in exclude
        for e in EvalSet.from_json((EVAL_DIR / f"eval_set_{other}.json").read_text()).examples
    }
    if excluded:
        print(f"Excluding {len(excluded)} wallets from {', '.join(exclude)}")

    async with httpx.AsyncClient(timeout=30) as http:
        client = client_from_settings(settings, http)
        cache = TransactionCache(sessions, client, ttl_seconds=settings.cache_ttl_seconds)

        head = await client.block_number()
        block = head - SNAPSHOT_LAG
        ts, _ = await client.block_senders(block)
        snapshot = Snapshot(block, datetime.datetime.fromtimestamp(ts, datetime.UTC))
        print(f"Snapshot: block {block} ({snapshot.time.isoformat()})")

        scoring, heldout = split_labels(flagged, seed)
        type_of = {label.address: label.label_type for label in flagged}
        print(f"Split: {len(scoring)} scoring / {len(heldout)} held-out flagged addresses")

        used = set(labeled) | excluded
        examples: list[Example] = []
        positive_groups: tuple[tuple[str, Group], ...] = (
            ("sanctioned", "pos_sanctioned"),
            ("mixer", "pos_mixer"),
        )
        for label_type, group in positive_groups:
            held = sorted(a for a in heldout if type_of[a] == label_type)
            rng.shuffle(held)
            examples += await _pick_positives(
                cache,
                client,
                held,
                TARGETS[group],
                used,
                snapshot,
                rng,
                group,
            )

        contaminated = 0
        attempts = 0
        random_negatives: list[Example] = []
        while (
            len(random_negatives) < TARGETS["neg_random"] and attempts < 10 * TARGETS["neg_random"]
        ):
            attempts += 1
            sampled = rng.randint(block - RANDOM_BLOCK_SPAN, block)
            _, senders = await client.block_senders(sampled)
            candidates = sorted(set(senders) - used)
            if not candidates:
                continue
            sender = rng.choice(candidates)
            used.add(sender)
            # Transacting with a held-out address makes it a positive by definition.
            neighbors = await _history_counterparties(
                cache, sender, snapshot, params.graph.max_records_target
            )
            if neighbors & heldout:
                contaminated += 1
                continue
            random_negatives.append(Example(sender, 0, "neg_random", f"block {sampled}"))
        examples += random_negatives

        exchange_pool = [a for a in exchanges if a not in excluded]
        rng.shuffle(exchange_pool)
        examples += [
            Example(address, 0, "neg_exchange", "etherscan-labels exchange")
            for address in exchange_pool[: TARGETS["neg_exchange"]]
        ]
    await get_engine().dispose()

    eval_set = EvalSet(
        version=version,
        created_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        seed=seed,
        snapshot_block=snapshot.block,
        snapshot_time=snapshot.time.isoformat(),
        label_sources=dict(sorted(Counter(r.source for r in rows).items())),
        flagged_labels=dict(sorted(type_of.items())),
        scoring_flagged=sorted(scoring),
        heldout_flagged=sorted(heldout),
        exchange_labels=exchanges,
        excluded_contaminated=contaminated,
        examples=examples,
    )
    out = EVAL_DIR / f"eval_set_{version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(eval_set.to_json())
    counts = Counter(e.group for e in examples)
    print(f"Wrote {out.name}: {len(examples)} examples {dict(counts)}")
    print(f"Random negatives dropped for touching a held-out address: {contaminated}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--exclude", nargs="*", default=[], help="eval set versions to avoid")
    args = parser.parse_args()
    asyncio.run(main(args.version, args.seed, args.exclude))
