"""Build a wallet's transaction graph by bounded breadth-first search over cached transfers.

Nodes are addresses; a directed edge u -> v aggregates every kept transfer from u to v.
BFS runs level by level up to `max_hops` (distance ignores direction: funds received
from a mixer count as much as funds sent to one). Guardrails, all from scoring.yaml:

- Labeled addresses (exchange, mixer, sanctioned, malicious) are recorded but never
  expanded. They are endpoints for scoring, and expanding a mixer pool or an exchange
  would pull in thousands of unrelated users.
- An unlabeled address whose history hits `max_records_per_node`, or that has more than
  `degree_threshold` counterparties, is a hub. It is recorded with its edges to addresses
  already in the graph, but its other counterparties are not added.
- At most `max_expanded_nodes` histories are fetched per graph, and each expansion queues
  at most `max_neighbors_per_node` unlabeled counterparties (most active first). Only
  labeled and queued counterparties become nodes; the rest are counted on the node as
  `skipped_neighbors`. `max_nodes` caps unlabeled nodes; labeled ones are always kept.
- Failed and zero-value transfers are skipped.
"""

import datetime
import logging
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Protocol

import networkx as nx

from app.clients.etherscan import Endpoint
from app.config import GraphParams
from app.core.addresses import normalize_address
from app.db.models import Transaction
from app.services.cache import CacheLookup

logger = logging.getLogger(__name__)

LabelIndex = Mapping[str, frozenset[str]]


class TransferLookup(Protocol):
    async def lookup(
        self, address: str, endpoint: Endpoint = ..., *, max_records: int | None = ...
    ) -> CacheLookup: ...


@dataclass
class GraphStats:
    expanded: int = 0
    lookups: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    hubs: int = 0
    budget_exhausted: bool = False
    node_limit_reached: bool = False


@dataclass
class TransactionGraph:
    root: str
    graph: nx.DiGraph
    stats: GraphStats = field(default_factory=GraphStats)

    def flagged_nodes(self, label_types: Iterable[str]) -> list[str]:
        wanted = set(label_types)
        return [n for n, labels in self.graph.nodes(data="labels") if wanted & set(labels)]


@dataclass
class _EdgeAgg:
    tx_count: int = 0
    total_value_wei: int = 0
    token_transfer_count: int = 0
    last_seen: int = 0

    def add(self, other: "_EdgeAgg") -> None:
        self.tx_count += other.tx_count
        self.total_value_wei += other.total_value_wei
        self.token_transfer_count += other.token_transfer_count
        self.last_seen = max(self.last_seen, other.last_seen)


@dataclass
class _NodeTransfers:
    """One node's kept transfers: edges not yet in the graph, plus activity per
    counterparty over *all* its transfers (for degree and neighbor ranking)."""

    new_edges: dict[tuple[str, str], _EdgeAgg] = field(default_factory=dict)
    activity: dict[str, tuple[int, int]] = field(default_factory=dict)

    def merge(self, other: "_NodeTransfers") -> None:
        for key, agg in other.new_edges.items():
            self.new_edges.setdefault(key, _EdgeAgg()).add(agg)
        for address, (count, value) in other.activity.items():
            c, v = self.activity.get(address, (0, 0))
            self.activity[address] = (c + count, v + value)


@dataclass
class _Run:
    """Per-build state, so one builder can serve concurrent builds."""

    result: TransactionGraph
    seen_transfers: set[tuple[str, str, str]] = field(default_factory=set)
    unlabeled_nodes: int = 0


class GraphBuilder:
    def __init__(self, lookup: TransferLookup, labels: LabelIndex, params: GraphParams) -> None:
        self._lookup = lookup
        self._labels = labels
        self._params = params

    async def build(self, root: str) -> TransactionGraph:
        root = normalize_address(root)
        run = _Run(TransactionGraph(root=root, graph=nx.DiGraph()))
        self._add_node(run, root, hop=0)

        level = [root]
        for hop in range(self._params.max_hops):
            next_level: list[str] = []
            for address in level:
                next_level.extend(await self._expand(run, address, hop))
            level = next_level
        for address in level:
            run.result.graph.nodes[address]["stop_reason"] = "hop_limit"
        return run.result

    async def _expand(self, run: _Run, address: str, hop: int) -> list[str]:
        """Fetch `address`, add its edges, and return the counterparties to expand next."""
        result, params = run.result, self._params
        graph, stats = result.graph, result.stats
        node = graph.nodes[address]
        is_root = address == result.root

        if node["labels"] and not is_root:
            node["stop_reason"] = "labeled"
            return []
        if stats.expanded >= params.max_expanded_nodes:
            node["stop_reason"] = "budget"
            stats.budget_exhausted = True
            return []

        cap = params.max_records_target if is_root else params.max_records_per_node
        rows, truncated = await self._fetch(stats, address, Endpoint.NORMAL, cap)
        transfers = self._collect(run, address, rows)
        if is_root or not self._is_hub(truncated, transfers):
            # Only fetch internal and token transfers once normal ones show it isn't a hub.
            for endpoint in (Endpoint.INTERNAL, Endpoint.TOKEN):
                more, more_truncated = await self._fetch(stats, address, endpoint, cap)
                truncated = truncated or more_truncated
                transfers.merge(self._collect(run, address, more))
        stats.expanded += 1

        node.update(expanded=True, degree=len(transfers.activity), truncated=truncated)
        if not is_root and self._is_hub(truncated, transfers):
            stats.hubs += 1
            node["stop_reason"] = "high_degree"
            self._add_edges(run, transfers.new_edges, hop + 1, keep=frozenset())
            return []

        return self._add_and_queue(run, address, transfers, hop + 1)

    def _is_hub(self, truncated: bool, transfers: _NodeTransfers) -> bool:
        return truncated or len(transfers.activity) > self._params.degree_threshold

    async def _fetch(
        self, stats: GraphStats, address: str, endpoint: Endpoint, cap: int
    ) -> tuple[list[Transaction], bool]:
        looked_up = await self._lookup.lookup(address, endpoint, max_records=cap)
        stats.lookups += 1
        stats.cache_hits += looked_up.cache_hit
        stats.api_calls += looked_up.upstream_calls
        return looked_up.transactions, looked_up.truncated

    def _collect(self, run: _Run, address: str, rows: Iterable[Transaction]) -> _NodeTransfers:
        out = _NodeTransfers()
        for row in rows:
            if self._params.skip_failed and row.is_error:
                continue
            if self._params.skip_zero_value and row.value_raw == 0:
                continue
            if row.from_addr == row.to_addr:
                continue
            is_eth = row.token_address is None
            eth_value = int(row.value_raw) if is_eth else 0
            other = row.to_addr if row.from_addr == address else row.from_addr
            count, value = out.activity.get(other, (0, 0))
            out.activity[other] = (count + 1, value + eth_value)

            # Both ends of a transfer may be expanded; count each transfer on one edge once.
            uid = (row.source_endpoint, row.hash, row.sub_key)
            if uid in run.seen_transfers:
                continue
            run.seen_transfers.add(uid)
            out.new_edges.setdefault((row.from_addr, row.to_addr), _EdgeAgg()).add(
                _EdgeAgg(
                    tx_count=1,
                    total_value_wei=eth_value,
                    token_transfer_count=0 if is_eth else 1,
                    last_seen=_epoch(row.timestamp),
                )
            )
        return out

    def _add_node(self, run: _Run, address: str, hop: int) -> bool:
        labels = self._labels.get(address, frozenset())
        if not labels:
            if run.unlabeled_nodes >= self._params.max_nodes:
                run.result.stats.node_limit_reached = True
                return False
            run.unlabeled_nodes += 1
        run.result.graph.add_node(
            address,
            hop=hop,
            labels=sorted(labels),
            expanded=False,
            degree=None,
            truncated=False,
            stop_reason=None,
            queued=False,
            skipped_neighbors=0,
        )
        return True

    def _add_edges(
        self,
        run: _Run,
        edges: Mapping[tuple[str, str], _EdgeAgg],
        new_hop: int,
        *,
        keep: AbstractSet[str],
    ) -> None:
        """Add `edges`, creating missing endpoints only if they are in `keep`."""
        graph = run.result.graph
        for (src, dst), agg in edges.items():
            if not all(
                a in graph or (a in keep and self._add_node(run, a, new_hop)) for a in (src, dst)
            ):
                continue
            if graph.has_edge(src, dst):
                existing = _EdgeAgg(**graph.edges[src, dst])
                existing.add(agg)
                graph.edges[src, dst].update(vars(existing))
            else:
                graph.add_edge(src, dst, **vars(agg))

    def _add_and_queue(
        self, run: _Run, address: str, transfers: _NodeTransfers, new_hop: int
    ) -> list[str]:
        """Add edges to labeled and to-be-expanded counterparties; return the queue.

        Unlabeled counterparties that won't be expanded are left out of the graph: they
        can't lead to a flagged address, and recording them let one busy wallet's direct
        counterparties fill `max_nodes` and starve deeper hops.
        """
        graph = run.result.graph
        nodes = graph.nodes
        activity = transfers.activity
        labeled = {n for n in activity if self._labels.get(n)}
        candidates = [
            n
            for n in activity
            if n not in labeled
            and (n not in graph or (nodes[n]["hop"] == new_hop and not nodes[n]["queued"]))
        ]
        candidates.sort(key=lambda n: (activity[n], n), reverse=True)
        picked = candidates[: self._params.max_neighbors_per_node]

        self._add_edges(run, transfers.new_edges, new_hop, keep=labeled | set(picked))

        queued = [n for n in picked if n in graph]
        for n in queued:
            nodes[n].update(queued=True, stop_reason=None)
        for n in labeled:
            if n in graph and nodes[n]["stop_reason"] is None:
                nodes[n]["stop_reason"] = "labeled"
        nodes[address]["skipped_neighbors"] = len(candidates) - len(queued)
        return queued


def _epoch(ts: datetime.datetime) -> int:
    return int(ts.timestamp())
