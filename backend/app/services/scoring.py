"""Hop-weighted risk scoring over a built transaction graph. See docs/SCORING.md.

For each flagged address f (sanctioned, malicious or mixer) reachable within `max_hops`:

    c_f = severity * hop_decay^(d-1) * flow_factor * discount
    score = 100 * (1 - prod(1 - c_f))

- d: hop distance of the chosen path (d = 0 when the target itself is flagged; weight 1).
- flow_factor = min(1, share / share_saturation), share = the path's bottleneck value
  (the smallest ETH-equivalent amount moved between any two consecutive addresses)
  / the target's total in+out volume.
- discount: 1 for a path avoiding exchanges and hubs; `exchange_discount` (or 0 in "cut"
  mode) for a path through one; always 1 when exchange handling is disabled.

Candidate paths are the shortest paths in the whole graph and the shortest paths that
avoid exchanges and hubs; the path with the largest contribution is reported. So a
discount never penalizes a wallet that also has an equally short clean path.
"""

import itertools
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

import networkx as nx

from app.config import ScoringParams
from app.services.graph import TransactionGraph

RISK_LABELS = ("sanctioned", "malicious", "mixer")
# Shortest paths can multiply combinatorially in dense graphs; this many is plenty to
# find the strongest one.
_MAX_PATHS_PER_TARGET = 50

SeverityOverrides = Mapping[tuple[str, str], float]


@dataclass(frozen=True)
class FlaggedContribution:
    address: str
    label: str
    labels: list[str]
    severity: float
    hops: int
    path: list[str]
    # Exchanges and hubs on the path (excluding its ends).
    via: list[str]
    bottleneck_eq_wei: int
    flow_share: float
    flow_factor: float
    hop_weight: float
    discount: float
    contribution: float


@dataclass(frozen=True)
class ScoreResult:
    address: str
    score: float
    bucket: str
    params_hash: str
    breakdown: list[FlaggedContribution]


def score_graph(
    graph: TransactionGraph,
    params: ScoringParams,
    severity_overrides: SeverityOverrides | None = None,
) -> ScoreResult:
    overrides = severity_overrides or {}
    g = graph.graph
    root = graph.root
    undirected = g.to_undirected(as_view=True)
    root_volume = g.nodes[root].get("volume_eq_wei") or 0

    breakdown: list[FlaggedContribution] = []
    for address in graph.flagged_nodes(RISK_LABELS):
        risk_labels = [t for t in g.nodes[address]["labels"] if t in RISK_LABELS]
        severities = {
            t: overrides.get((address, t), getattr(params.severity, t)) for t in risk_labels
        }
        label = max(severities, key=lambda t: severities[t])
        best = _best_path(g, undirected, root, address, root_volume, params)
        if best is None:
            continue
        path, via, bottleneck, share, flow_factor, hop_weight, discount = best
        breakdown.append(
            FlaggedContribution(
                address=address,
                label=label,
                labels=risk_labels,
                severity=severities[label],
                hops=len(path) - 1,
                path=path,
                via=via,
                bottleneck_eq_wei=bottleneck,
                flow_share=share,
                flow_factor=flow_factor,
                hop_weight=hop_weight,
                discount=discount,
                contribution=severities[label] * hop_weight * flow_factor * discount,
            )
        )

    breakdown.sort(key=lambda c: (-c.contribution, c.hops, c.address))
    remaining = 1.0
    for item in breakdown:
        remaining *= 1 - item.contribution
    score = round(100 * (1 - remaining), 2)
    return ScoreResult(
        address=root,
        score=score,
        bucket=_bucket(score, params),
        params_hash=params.params_hash,
        breakdown=breakdown,
    )


_PathScore = tuple[list[str], list[str], int, float, float, float, float]


def _best_path(
    g: nx.DiGraph,
    undirected: nx.Graph,
    root: str,
    target: str,
    root_volume: int,
    params: ScoringParams,
) -> _PathScore | None:
    if target == root:
        return [root], [], root_volume, 1.0, 1.0, 1.0, 1.0

    via_nodes = {
        n
        for n, data in undirected.nodes(data=True)
        if n not in (root, target)
        and ("exchange" in data["labels"] or data.get("stop_reason") == "high_degree")
    }
    clean_view = nx.subgraph_view(undirected, filter_node=lambda n: n not in via_nodes)
    handling = params.exchange_handling

    best: _PathScore | None = None
    best_value = -1.0
    for path in itertools.chain(
        _shortest_paths(undirected, root, target, params.max_hops),
        _shortest_paths(clean_view, root, target, params.max_hops),
    ):
        via = [n for n in path[1:-1] if n in via_nodes]
        if via and handling.enabled:
            discount = handling.exchange_discount if handling.mode == "discount" else 0.0
        else:
            discount = 1.0
        bottleneck = _bottleneck(g, path)
        share = bottleneck / root_volume if root_volume > 0 else 0.0
        flow_factor = min(1.0, share / params.flow.share_saturation)
        hop_weight = params.hop_decay ** (len(path) - 2)
        value = hop_weight * flow_factor * discount
        if value > best_value:
            best_value = value
            best = (path, via, bottleneck, share, flow_factor, hop_weight, discount)
    return best


def _shortest_paths(g: nx.Graph, source: str, target: str, max_hops: int) -> Iterator[list[str]]:
    try:
        paths = nx.all_shortest_paths(g, source, target)
        first = next(paths)
    except (nx.NetworkXNoPath, nx.NodeNotFound, StopIteration):
        return
    if len(first) - 1 > max_hops:
        return
    yield first
    yield from itertools.islice(paths, _MAX_PATHS_PER_TARGET - 1)


def _bottleneck(g: nx.DiGraph, path: Sequence[str]) -> int:
    """Smallest value moved between consecutive addresses (both directions summed)."""
    return min(_pair_value(g, u, v) for u, v in itertools.pairwise(path))


def _pair_value(g: nx.DiGraph, u: str, v: str) -> int:
    return sum(
        int(g.edges[a, b].get("value_eq_wei", 0)) for a, b in ((u, v), (v, u)) if g.has_edge(a, b)
    )


def _bucket(score: float, params: ScoringParams) -> str:
    buckets = params.buckets
    for name, lower in (
        ("severe", buckets.severe),
        ("high", buckets.high),
        ("medium", buckets.medium),
    ):
        if score >= lower:
            return name
    return "low"


def breakdown_json(result: ScoreResult) -> list[dict[str, object]]:
    """Breakdown as JSON-safe dicts. Wei amounts are strings: they exceed 2^53."""
    return [
        {**vars(item), "bottleneck_eq_wei": str(item.bottleneck_eq_wei)}
        for item in result.breakdown
    ]


class SubgraphNode(TypedDict):
    address: str
    role: Literal["target", "flagged", "path"]
    labels: list[str]
    hop: int | None
    is_hub: bool


class SubgraphEdge(TypedDict):
    source: str
    target: str
    tx_count: int
    token_transfer_count: int
    total_value_wei: str
    value_eq_wei: str
    last_seen: int | None


class Subgraph(TypedDict):
    nodes: list[SubgraphNode]
    edges: list[SubgraphEdge]


def flagged_subgraph(graph: TransactionGraph, result: ScoreResult) -> Subgraph:
    """Only the nodes and edges on the breakdown's paths, JSON-safe, for display."""
    g = graph.graph
    flagged = {item.address for item in result.breakdown}
    on_path: dict[str, None] = {}
    pairs: dict[tuple[str, str], None] = {}
    for item in result.breakdown:
        on_path.update(dict.fromkeys(item.path))
        for u, v in itertools.pairwise(item.path):
            for a, b in ((u, v), (v, u)):
                if g.has_edge(a, b):
                    pairs[(a, b)] = None

    nodes: list[SubgraphNode] = []
    for address in on_path:
        data = g.nodes[address]
        role: Literal["target", "flagged", "path"] = (
            "target" if address == graph.root else "flagged" if address in flagged else "path"
        )
        nodes.append(
            {
                "address": address,
                "role": role,
                "labels": list(data["labels"]),
                "hop": data.get("hop"),
                "is_hub": data.get("stop_reason") == "high_degree",
            }
        )
    edges: list[SubgraphEdge] = []
    for a, b in pairs:
        data = g.edges[a, b]
        edges.append(
            {
                "source": a,
                "target": b,
                "tx_count": data.get("tx_count", 0),
                "token_transfer_count": data.get("token_transfer_count", 0),
                "total_value_wei": str(data.get("total_value_wei", 0)),
                "value_eq_wei": str(data.get("value_eq_wei", 0)),
                "last_seen": data.get("last_seen"),
            }
        )
    return {"nodes": nodes, "edges": edges}
