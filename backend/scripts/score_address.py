"""Score one address: build its graph, apply hop-weighted scoring, print the breakdown.

Usage (from backend/): python scripts/score_address.py 0xADDRESS
Needs ETHERSCAN_API_KEY and ingested labels.
"""

import argparse
import asyncio
import sys
import time

import httpx

from app.clients.etherscan import client_from_settings
from app.config import get_settings, load_scoring_params
from app.db.session import get_engine, get_sessionmaker
from app.services.cache import TransactionCache
from app.services.graph import GraphBuilder
from app.services.labels import load_label_index, load_severity_overrides
from app.services.scoring import score_graph
from app.services.valuation import make_valuer

_WEI = 10**18


async def main(address: str) -> None:
    settings = get_settings()
    params = load_scoring_params()
    sessions = get_sessionmaker()
    async with sessions() as session:
        labels = await load_label_index(session)
        overrides = await load_severity_overrides(session)
    if not labels:
        sys.exit("No labels loaded; run scripts/ingest_ofac.py and scripts/ingest_labels.py.")

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as http:
        cache = TransactionCache(
            sessions, client_from_settings(settings, http), ttl_seconds=settings.cache_ttl_seconds
        )
        builder = GraphBuilder(cache, labels, params.graph, make_valuer(params.valuation))
        graph = await builder.build(address)
    result = score_graph(graph, params, overrides)
    elapsed = time.perf_counter() - started
    await get_engine().dispose()

    stats = graph.stats
    volume = (graph.graph.nodes[graph.root].get("volume_eq_wei") or 0) / _WEI
    print(f"{result.address}")
    print(f"  score {result.score}  ({result.bucket})   params {result.params_hash}")
    print(
        f"  graph: {graph.graph.number_of_nodes()} nodes, {stats.expanded} expanded, "
        f"{stats.api_calls} API calls, {elapsed:.1f}s; target volume {volume:,.2f} ETH-eq"
    )
    if not result.breakdown:
        print("  no flagged addresses within range")
    for c in result.breakdown:
        via = f" via {len(c.via)} exchange/hub" if c.via else ""
        print(
            f"  {c.contribution:6.3f}  {c.label:10} hop {c.hops}{via}  {c.address}\n"
            f"          severity {c.severity} x hop {c.hop_weight:.3f} x flow {c.flow_factor:.3f}"
            f" (share {c.flow_share:.2%}, bottleneck {c.bottleneck_eq_wei / _WEI:,.4f} ETH-eq)"
            f" x discount {c.discount}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    asyncio.run(main(parser.parse_args().address))
