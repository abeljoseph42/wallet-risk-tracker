"""Build the transaction graph for one address and report what the traversal did.

Usage (from backend/): python scripts/build_graph.py 0xADDRESS
Needs ETHERSCAN_API_KEY. Labels must be ingested first (ingest_ofac.py, ingest_labels.py).
"""

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter

import httpx
from sqlalchemy import select

from app.clients.etherscan import client_from_settings
from app.config import get_settings, load_graph_params
from app.db.models import AddressLabel
from app.db.session import get_engine, get_sessionmaker
from app.services.cache import TransactionCache
from app.services.graph import GraphBuilder
from app.services.labels import load_label_index


class _RetryCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.rate_limited = 0
        self.other = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "rate limited" in record.getMessage():
            self.rate_limited += 1
        else:
            self.other += 1


async def main(address: str) -> None:
    settings = get_settings()
    if not settings.etherscan_api_key:
        sys.exit("ETHERSCAN_API_KEY is not set.")
    params = load_graph_params()
    retries = _RetryCounter()
    logging.getLogger("app.clients.etherscan").addHandler(retries)

    sessions = get_sessionmaker()
    async with sessions() as session:
        labels = await load_label_index(session)
    if not labels:
        sys.exit("No labels loaded; run scripts/ingest_ofac.py and scripts/ingest_labels.py.")

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as http:
        client = client_from_settings(settings, http)
        cache = TransactionCache(sessions, client, ttl_seconds=settings.cache_ttl_seconds)
        result = await GraphBuilder(cache, labels, params).build(address)
    elapsed = time.perf_counter() - started

    graph, stats = result.graph, result.stats
    flagged = result.flagged_nodes(["sanctioned", "malicious", "mixer"])
    async with sessions() as session:
        names = {
            (row.address, row.label_type): row.name
            for row in await session.scalars(
                select(AddressLabel).where(AddressLabel.address.in_(flagged))
            )
        }
    await get_engine().dispose()

    print(f"Graph for {result.root}  ({params.max_hops} hops)")
    print(f"  nodes {graph.number_of_nodes()}, edges {graph.number_of_edges()}")
    print(f"  expanded {stats.expanded}/{params.max_expanded_nodes}, hubs {stats.hubs}")
    print(f"  budget exhausted {stats.budget_exhausted}, node limit hit {stats.node_limit_reached}")
    print(f"  lookups {stats.lookups} (cache hits {stats.cache_hits}), API calls {stats.api_calls}")
    print(f"  rate-limit retries {retries.rate_limited}, other retries {retries.other}")
    print(f"  wall time {elapsed:.1f}s")
    reasons = Counter(r for _, r in graph.nodes(data="stop_reason") if r)
    print(f"  not expanded: {dict(reasons.most_common())}")
    labeled = Counter(t for _, ls in graph.nodes(data="labels") for t in ls)
    print(f"  labeled nodes: {dict(labeled)}")
    print(f"Flagged addresses ({len(flagged)}):")
    for node in sorted(flagged, key=lambda n: graph.nodes[n]["hop"]):
        for label_type in graph.nodes[node]["labels"]:
            name = names.get((node, label_type)) or ""
            print(f"  hop {graph.nodes[node]['hop']}  {label_type:10} {node}  {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    asyncio.run(main(parser.parse_args().address))
