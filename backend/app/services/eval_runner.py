"""Build every eval-set wallet's graph at the pinned snapshot, for run_eval and tuning.

Graphs depend only on labels and graph parameters, so they're built once per label
mode and traversal variant; scoring variants (hop weighting, flow factor, exchange
handling, threshold) are then applied to the same graphs, with no further API calls.
"""

import datetime
import time
from dataclasses import dataclass, field

import httpx

from app.clients.etherscan import EtherscanError, client_from_settings
from app.config import GraphParams, ScoringParams, get_settings
from app.db.session import get_sessionmaker
from app.services.cache import Snapshot, TransactionCache
from app.services.evaluation import EvalSet
from app.services.graph import GraphBuilder, TransactionGraph
from app.services.valuation import make_valuer

MODES = ("holdout", "in_sample")


def label_index(eval_set: EvalSet, mode: str) -> dict[str, frozenset[str]]:
    """Hold-out: the scorer sees only the scoring half of the flagged labels."""
    flagged = eval_set.scoring_flagged if mode == "holdout" else list(eval_set.flagged_labels)
    index: dict[str, set[str]] = {a: {"exchange"} for a in eval_set.exchange_labels}
    for address in flagged:
        index.setdefault(address, set()).add(eval_set.flagged_labels[address])
    return {a: frozenset(t) for a, t in index.items()}


def scoring_variants(params: ScoringParams) -> dict[str, ScoringParams]:
    """The full model and ablations that differ only in scoring (graphs are shared)."""
    no_hop = {"hop_decay": 1.0}
    no_flow = {"flow": params.flow.model_copy(update={"enabled": False})}
    no_exchange = {
        "exchange_handling": params.exchange_handling.model_copy(update={"enabled": False})
    }
    return {
        "full": params,
        "no_hop_weighting": params.model_copy(update=no_hop),
        "no_flow_factor": params.model_copy(update=no_flow),
        "no_exchange_handling": params.model_copy(update=no_exchange),
        "naive_scoring": params.model_copy(update={**no_hop, **no_flow, **no_exchange}),
    }


def naive_traversal(params: ScoringParams) -> tuple[GraphParams, ScoringParams]:
    """Traverse through exchanges and hubs, and score with every discount off."""
    graph = params.graph.model_copy(update={"expand_exchanges": True, "expand_hubs": True})
    naive = scoring_variants(params)["naive_scoring"].model_copy(update={"graph": graph})
    return graph, naive


def snapshot_of(eval_set: EvalSet) -> Snapshot:
    return Snapshot(
        eval_set.snapshot_block, datetime.datetime.fromisoformat(eval_set.snapshot_time)
    )


@dataclass
class GraphRun:
    graphs: dict[str, TransactionGraph] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    api_calls: int = 0


async def build_graphs(
    eval_set: EvalSet,
    labels: dict[str, frozenset[str]],
    graph_params: GraphParams,
    params: ScoringParams,
    *,
    name: str,
) -> GraphRun:
    settings = get_settings()
    run = GraphRun()
    async with httpx.AsyncClient(timeout=30) as http:
        cache = TransactionCache(
            get_sessionmaker(),
            client_from_settings(settings, http),
            ttl_seconds=settings.cache_ttl_seconds,
        )
        builder = GraphBuilder(
            cache,
            labels,
            graph_params,
            make_valuer(params.valuation),
            snapshot=snapshot_of(eval_set),
        )
        started = time.perf_counter()
        total = len(eval_set.examples)
        for i, example in enumerate(eval_set.examples, start=1):
            try:
                graph = await builder.build(example.address)
            except EtherscanError as exc:
                print(f"  {name}: {example.address} failed: {exc.kind}: {exc}")
                run.failed.add(example.address)
                continue
            run.graphs[example.address] = graph
            run.api_calls += graph.stats.api_calls
            if i % 25 == 0 or i == total:
                print(
                    f"  {name}: {i}/{total} wallets, {run.api_calls} API calls, "
                    f"{time.perf_counter() - started:.0f}s",
                    flush=True,
                )
    return run
