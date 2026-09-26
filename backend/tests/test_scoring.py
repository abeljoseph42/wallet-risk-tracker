"""Scoring tests on hand-built graphs, so each case isolates one part of the formula."""

import networkx as nx
import pytest

from app.config import ScoringParams, load_scoring_params
from app.services.graph import TransactionGraph
from app.services.scoring import breakdown_json, flagged_subgraph, score_graph

ETH = 10**18
ROOT = "0x" + "1" * 40
A = "0x" + "a" * 40
B = "0x" + "b" * 40
SANCTIONED = "0x" + "5" * 40
MIXER = "0x" + "6" * 40
EXCHANGE = "0x" + "e" * 40
HUB = "0x" + "d" * 40

LABELS = {SANCTIONED: ["sanctioned"], MIXER: ["mixer"], EXCHANGE: ["exchange"]}


def make_graph(
    edges: list[tuple[str, str, int]],
    *,
    root_volume: int | None = None,
    root_labels: list[str] | None = None,
    hubs: tuple[str, ...] = (),
) -> TransactionGraph:
    """Edges are (src, dst, value in wei). Root volume defaults to the root's edge total."""
    g = nx.DiGraph()
    g.add_node(ROOT, labels=root_labels or [], stop_reason=None)
    for src, dst, value in edges:
        for node in (src, dst):
            if node not in g:
                g.add_node(
                    node,
                    labels=LABELS.get(node, []),
                    stop_reason="high_degree" if node in hubs else None,
                )
        g.add_edge(src, dst, value_eq_wei=value)
    if root_volume is None:
        root_volume = sum(v for s, d, v in edges if ROOT in (s, d))
    g.nodes[ROOT]["volume_eq_wei"] = root_volume
    return TransactionGraph(root=ROOT, graph=g)


@pytest.fixture
def params() -> ScoringParams:
    return load_scoring_params()


def with_handling(params: ScoringParams, **changes: object) -> ScoringParams:
    handling = params.exchange_handling.model_copy(update=changes)
    return params.model_copy(update={"exchange_handling": handling})


def test_no_flagged_neighbors_scores_zero(params: ScoringParams) -> None:
    result = score_graph(make_graph([(ROOT, A, ETH), (A, B, ETH)]), params)

    assert result.score == 0
    assert result.bucket == "low"
    assert result.breakdown == []


def test_directly_sanctioned_target_scores_100(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, A, ETH)], root_labels=["sanctioned"])

    result = score_graph(graph, params)

    assert result.score == 100
    assert result.bucket == "severe"
    assert result.breakdown[0].hops == 0
    assert result.breakdown[0].path == [ROOT]


def test_one_hop_sanctioned_with_full_flow_scores_100(params: ScoringParams) -> None:
    result = score_graph(make_graph([(ROOT, SANCTIONED, ETH)]), params)

    item = result.breakdown[0]
    assert (item.hops, item.hop_weight, item.flow_factor) == (1, 1.0, 1.0)
    assert result.score == 100


def test_two_hops_is_half_weight(params: ScoringParams) -> None:
    result = score_graph(make_graph([(ROOT, A, ETH), (A, SANCTIONED, ETH)]), params)

    assert result.breakdown[0].hops == 2
    assert result.score == 50
    assert result.bucket == "high"


def test_three_hops_is_quarter_weight(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, A, ETH), (A, B, ETH), (B, SANCTIONED, ETH)])

    result = score_graph(graph, params)

    assert result.breakdown[0].hops == 3
    assert result.score == 25
    assert result.bucket == "medium"


def test_beyond_max_hops_is_ignored(params: ScoringParams) -> None:
    c = "0x" + "c" * 40
    graph = make_graph([(ROOT, A, ETH), (A, B, ETH), (B, c, ETH), (c, SANCTIONED, ETH)])

    assert score_graph(graph, params).breakdown == []


def test_mixer_uses_mixer_severity(params: ScoringParams) -> None:
    result = score_graph(make_graph([(MIXER, ROOT, ETH)]), params)

    assert result.breakdown[0].label == "mixer"
    assert result.score == 70


def test_incoming_link_counts_the_same_as_outgoing(params: ScoringParams) -> None:
    incoming = score_graph(make_graph([(SANCTIONED, ROOT, ETH)]), params)
    outgoing = score_graph(make_graph([(ROOT, SANCTIONED, ETH)]), params)

    assert incoming.score == outgoing.score


def test_dust_relative_to_volume_barely_counts(params: ScoringParams) -> None:
    # 0.001 ETH with a sanctioned address out of 100 ETH volume: share 1e-5.
    graph = make_graph([(SANCTIONED, ROOT, ETH // 1000), (ROOT, A, 100 * ETH)])

    item = score_graph(graph, params).breakdown[0]

    assert item.flow_share == pytest.approx(0.001 / 100.001)
    assert item.contribution < 0.001


def test_flow_factor_saturates_at_share_saturation(params: ScoringParams) -> None:
    # 5% of volume with default saturation 0.10 -> flow_factor 0.5.
    graph = make_graph([(ROOT, SANCTIONED, 5 * ETH), (ROOT, A, 95 * ETH)])

    item = score_graph(graph, params).breakdown[0]

    assert item.flow_factor == pytest.approx(0.5)
    assert score_graph(graph, params).score == 50


def test_bottleneck_is_the_smallest_hop_on_the_path(params: ScoringParams) -> None:
    # Root sent A only 1 ETH of its 10 ETH volume; A then sent 1000 ETH to the mixer.
    graph = make_graph([(ROOT, A, ETH), (ROOT, B, 9 * ETH), (A, MIXER, 1000 * ETH)])

    item = score_graph(graph, params).breakdown[0]

    assert item.bottleneck_eq_wei == ETH
    assert item.flow_share == pytest.approx(0.1)


def test_zero_volume_target_gets_no_flow(params: ScoringParams) -> None:
    graph = make_graph([(SANCTIONED, ROOT, 0)], root_volume=0)

    assert score_graph(graph, params).score == 0


def test_path_through_exchange_is_discounted(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, EXCHANGE, ETH), (EXCHANGE, SANCTIONED, ETH)])

    item = score_graph(graph, params).breakdown[0]

    assert item.via == [EXCHANGE]
    assert item.discount == 0.2
    assert score_graph(graph, params).score == 10  # 1.0 * 0.5 * 1.0 * 0.2


def test_path_through_hub_is_discounted_like_an_exchange(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, HUB, ETH), (HUB, SANCTIONED, ETH)], hubs=(HUB,))

    item = score_graph(graph, params).breakdown[0]

    assert item.via == [HUB]
    assert item.discount == 0.2


def test_longer_clean_path_beats_discounted_short_path(params: ScoringParams) -> None:
    graph = make_graph(
        [
            (ROOT, EXCHANGE, ETH),
            (EXCHANGE, SANCTIONED, ETH),  # 2 hops via exchange: 0.5 * 0.2 = 0.1
            (ROOT, A, ETH),
            (A, B, ETH),
            (B, SANCTIONED, ETH),  # 3 hops clean: 0.25
        ],
        root_volume=2 * ETH,
    )

    item = score_graph(graph, params).breakdown[0]

    assert item.path == [ROOT, A, B, SANCTIONED]
    assert item.via == []
    assert item.contribution == pytest.approx(0.25)


def test_cut_mode_drops_paths_through_exchanges(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, EXCHANGE, ETH), (EXCHANGE, SANCTIONED, ETH)])

    result = score_graph(graph, with_handling(params, mode="cut"))

    assert result.breakdown[0].discount == 0
    assert result.score == 0


def test_disabled_handling_applies_no_discount(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, EXCHANGE, ETH), (EXCHANGE, SANCTIONED, ETH)])

    result = score_graph(graph, with_handling(params, enabled=False))

    assert result.breakdown[0].via == [EXCHANGE]
    assert result.breakdown[0].discount == 1
    assert result.score == 50


def test_multiple_weak_signals_combine_but_stay_bounded(params: ScoringParams) -> None:
    graph = make_graph(
        [(ROOT, A, ETH), (A, SANCTIONED, ETH), (ROOT, B, ETH), (B, MIXER, ETH)],
    )

    result = score_graph(graph, params)

    # 0.5 (2-hop sanctioned) and 0.35 (2-hop mixer): 1 - 0.5 * 0.65 = 0.675
    assert [c.address for c in result.breakdown] == [SANCTIONED, MIXER]
    assert result.score == 67.5


def test_severity_override_replaces_the_label_default(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, MIXER, ETH)])

    result = score_graph(graph, params, severity_overrides={(MIXER, "mixer"): 0.3})

    assert result.breakdown[0].severity == 0.3
    assert result.score == 30


def test_address_with_two_risk_labels_uses_the_more_severe(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, MIXER, ETH)])
    graph.graph.nodes[MIXER]["labels"] = ["mixer", "sanctioned"]

    item = score_graph(graph, params).breakdown[0]

    assert item.label == "sanctioned"
    assert item.labels == ["mixer", "sanctioned"]


def test_exchange_label_alone_is_not_a_risk(params: ScoringParams) -> None:
    assert score_graph(make_graph([(ROOT, EXCHANGE, ETH)]), params).score == 0


def test_result_carries_params_hash(params: ScoringParams) -> None:
    result = score_graph(make_graph([(ROOT, A, ETH)]), params)

    assert result.params_hash == params.params_hash


def test_flagged_subgraph_contains_only_breakdown_paths(params: ScoringParams) -> None:
    graph = make_graph([(ROOT, A, ETH), (A, SANCTIONED, ETH), (ROOT, B, 2 * ETH)])
    result = score_graph(graph, params)

    sub = flagged_subgraph(graph, result)

    nodes = {n["address"]: n for n in sub["nodes"]}
    assert set(nodes) == {ROOT, A, SANCTIONED}
    assert nodes[ROOT]["role"] == "target"
    assert nodes[A]["role"] == "path"
    assert nodes[SANCTIONED]["role"] == "flagged"
    assert {(e["source"], e["target"]) for e in sub["edges"]} == {(ROOT, A), (A, SANCTIONED)}


def test_breakdown_json_stringifies_wei(params: ScoringParams) -> None:
    result = score_graph(make_graph([(ROOT, SANCTIONED, 10**24)]), params)

    item = breakdown_json(result)[0]

    assert item["bottleneck_eq_wei"] == str(10**24)
    assert item["address"] == SANCTIONED
