"""Graph builder tests on small synthetic transfer graphs (no DB, no network)."""

import datetime
import itertools
from decimal import Decimal

import pytest

from app.clients.etherscan import Endpoint
from app.config import GraphParams
from app.db.models import Transaction
from app.services.cache import CacheLookup
from app.services.graph import GraphBuilder, LabelIndex

_hashes = itertools.count(1)


def a(n: int) -> str:
    return f"0x{n:040x}"


ROOT = a(1)
SANCTIONED = a(900)
MIXER = a(901)
EXCHANGE = a(902)


def tx(
    src: str,
    dst: str,
    value: int = 10**18,
    *,
    endpoint: Endpoint = Endpoint.NORMAL,
    is_error: bool = False,
    ts: int = 1_700_000_000,
) -> Transaction:
    return Transaction(
        source_endpoint=endpoint.value,
        hash=f"0x{next(_hashes):064x}",
        sub_key="",
        from_addr=src,
        to_addr=dst,
        value_raw=Decimal(value),
        token_address=a(777) if endpoint is Endpoint.TOKEN else None,
        token_symbol=None,
        token_decimals=None,
        block_number=1,
        timestamp=datetime.datetime.fromtimestamp(ts, datetime.UTC),
        is_error=is_error,
    )


class FakeLookup:
    def __init__(self, transfers: list[Transaction]) -> None:
        self.transfers = transfers
        self.calls: list[tuple[str, Endpoint]] = []

    async def lookup(
        self, address: str, endpoint: Endpoint = Endpoint.NORMAL, *, max_records: int | None = None
    ) -> CacheLookup:
        self.calls.append((address, endpoint))
        rows = [
            t
            for t in self.transfers
            if t.source_endpoint == endpoint.value and address in (t.from_addr, t.to_addr)
        ]
        truncated = max_records is not None and len(rows) > max_records
        if truncated:
            rows = rows[:max_records]
        return CacheLookup(rows, cache_hit=False, upstream_calls=1, truncated=truncated)

    def expanded(self) -> set[str]:
        return {address for address, _ in self.calls}


LABELS: LabelIndex = {
    SANCTIONED: frozenset({"sanctioned"}),
    MIXER: frozenset({"mixer"}),
    EXCHANGE: frozenset({"exchange"}),
}


def params(**overrides: int | bool) -> GraphParams:
    base: dict[str, int | bool] = {
        "max_hops": 3,
        "max_expanded_nodes": 100,
        "max_neighbors_per_node": 10,
        "max_nodes": 2000,
        "max_records_per_node": 2000,
        "max_records_target": 20000,
        "degree_threshold": 500,
    }
    base.update(overrides)
    return GraphParams.model_validate(base)


async def test_direct_sanctioned_counterparty_is_hop_one_and_not_expanded() -> None:
    lookup = FakeLookup([tx(ROOT, SANCTIONED)])
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    node = result.graph.nodes[SANCTIONED]
    assert node["hop"] == 1
    assert node["labels"] == ["sanctioned"]
    assert node["stop_reason"] == "labeled"
    assert lookup.expanded() == {ROOT}
    assert result.flagged_nodes(["sanctioned"]) == [SANCTIONED]


async def test_finds_flagged_addresses_at_two_and_three_hops_but_not_four() -> None:
    far = a(903)
    lookup = FakeLookup(
        [
            tx(ROOT, a(2)),
            tx(a(2), a(3)),
            tx(a(3), MIXER),  # hop 3
            tx(a(3), a(4)),
            tx(a(4), far),  # would be hop 4
        ]
    )
    labels = {**LABELS, far: frozenset({"sanctioned"})}
    result = await GraphBuilder(lookup, labels, params()).build(ROOT)

    hops = dict(result.graph.nodes(data="hop"))
    assert hops[a(2)] == 1
    assert hops[a(3)] == 2
    assert hops[MIXER] == 3
    assert far not in result.graph
    assert result.graph.nodes[a(4)]["stop_reason"] == "hop_limit"
    assert a(4) not in lookup.expanded()


async def test_incoming_mixer_withdrawal_via_internal_transfer_is_found() -> None:
    lookup = FakeLookup([tx(MIXER, ROOT, endpoint=Endpoint.INTERNAL)])
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    assert result.graph.has_edge(MIXER, ROOT)
    assert result.graph.nodes[MIXER]["hop"] == 1


async def test_labeled_exchange_is_recorded_but_not_expanded_through() -> None:
    lookup = FakeLookup([tx(ROOT, EXCHANGE), tx(EXCHANGE, SANCTIONED)])
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    assert result.graph.nodes[EXCHANGE]["stop_reason"] == "labeled"
    assert SANCTIONED not in result.graph
    assert lookup.expanded() == {ROOT}


async def test_high_degree_hub_keeps_existing_edges_but_adds_no_new_counterparties() -> None:
    hub = a(50)
    fan_out = [tx(hub, a(1000 + i)) for i in range(5)]
    lookup = FakeLookup(
        [tx(ROOT, hub), tx(ROOT, a(2)), tx(a(2), hub), *fan_out, tx(hub, SANCTIONED)]
    )
    result = await GraphBuilder(lookup, LABELS, params(degree_threshold=4)).build(ROOT)

    node = result.graph.nodes[hub]
    assert node["stop_reason"] == "high_degree"
    assert node["degree"] == 8
    assert SANCTIONED not in result.graph
    assert a(1000) not in result.graph
    assert result.graph.has_edge(a(2), hub)
    # Normal transfers already showed it's a hub, so internal/token were never fetched.
    assert [e for addr, e in lookup.calls if addr == hub] == [Endpoint.NORMAL]
    assert result.stats.hubs == 1


async def test_neighbor_hitting_the_record_cap_is_treated_as_a_hub() -> None:
    busy = a(60)
    lookup = FakeLookup([tx(ROOT, busy), *(tx(busy, a(2000 + i)) for i in range(3))])
    result = await GraphBuilder(lookup, LABELS, params(max_records_per_node=2)).build(ROOT)

    assert result.graph.nodes[busy]["truncated"]
    assert result.graph.nodes[busy]["stop_reason"] == "high_degree"


async def test_root_is_expanded_even_when_it_is_high_degree() -> None:
    lookup = FakeLookup([tx(ROOT, a(10 + i)) for i in range(6)] + [tx(a(10), SANCTIONED)])
    result = await GraphBuilder(lookup, LABELS, params(degree_threshold=2)).build(ROOT)

    assert result.graph.nodes[ROOT]["expanded"]
    assert result.graph.nodes[ROOT]["stop_reason"] is None
    assert SANCTIONED in result.graph


async def test_failed_and_zero_value_transfers_are_skipped() -> None:
    poisoner = a(70)
    lookup = FakeLookup(
        [
            tx(SANCTIONED, ROOT, value=0),  # address poisoning / dust
            tx(ROOT, MIXER, is_error=True),
            tx(ROOT, poisoner, value=0, endpoint=Endpoint.TOKEN),
        ]
    )
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    assert list(result.graph.nodes) == [ROOT]


async def test_zero_value_and_failed_can_be_kept_when_configured() -> None:
    lookup = FakeLookup([tx(SANCTIONED, ROOT, value=0), tx(ROOT, MIXER, is_error=True)])
    result = await GraphBuilder(
        lookup, LABELS, params(skip_zero_value=False, skip_failed=False)
    ).build(ROOT)

    assert {SANCTIONED, MIXER} <= set(result.graph.nodes)


async def test_only_the_most_active_neighbors_are_expanded() -> None:
    transfers = [tx(ROOT, a(10 + i)) for i in range(4)]
    transfers += [tx(ROOT, a(12)), tx(a(12), ROOT), tx(ROOT, a(13))]  # a(12)=3 txs, a(13)=2
    lookup = FakeLookup(transfers)
    result = await GraphBuilder(lookup, LABELS, params(max_neighbors_per_node=2)).build(ROOT)

    assert lookup.expanded() == {ROOT, a(12), a(13)}
    # Unlabeled neighbors that won't be expanded aren't recorded, only counted.
    assert a(10) not in result.graph
    assert result.graph.nodes[ROOT]["skipped_neighbors"] == 2


async def test_labeled_neighbors_are_recorded_even_past_the_neighbor_cap() -> None:
    transfers = [tx(ROOT, a(10 + i)) for i in range(3)] + [tx(ROOT, EXCHANGE)]
    lookup = FakeLookup(transfers)
    result = await GraphBuilder(lookup, LABELS, params(max_neighbors_per_node=1)).build(ROOT)

    assert EXCHANGE in result.graph
    assert result.graph.nodes[EXCHANGE]["stop_reason"] == "labeled"


async def test_a_busy_first_hop_does_not_starve_deeper_hops() -> None:
    # 50 idle neighbors plus one active one that leads to a mixer two hops out.
    transfers = [tx(ROOT, a(100 + i)) for i in range(50)]
    transfers += [tx(ROOT, a(2)), tx(ROOT, a(2)), tx(a(2), a(3)), tx(a(3), MIXER)]
    lookup = FakeLookup(transfers)
    result = await GraphBuilder(
        lookup, LABELS, params(max_neighbors_per_node=1, max_nodes=5)
    ).build(ROOT)

    assert result.graph.nodes[MIXER]["hop"] == 3
    assert not result.stats.node_limit_reached


async def test_expansion_budget_stops_fetching() -> None:
    lookup = FakeLookup([tx(ROOT, a(10 + i)) for i in range(5)])
    result = await GraphBuilder(lookup, LABELS, params(max_expanded_nodes=3)).build(ROOT)

    assert result.stats.expanded == 3
    assert result.stats.budget_exhausted
    assert len(lookup.expanded()) == 3
    # Root plus 2 of its 5 neighbors were expanded; the other 3 hit the budget.
    stopped = [n for n, r in result.graph.nodes(data="stop_reason") if r == "budget"]
    assert len(stopped) == 3


async def test_node_limit_caps_unlabeled_nodes_but_keeps_labeled_ones() -> None:
    lookup = FakeLookup([*(tx(ROOT, a(10 + i)) for i in range(5)), tx(ROOT, SANCTIONED)])
    result = await GraphBuilder(lookup, LABELS, params(max_nodes=3)).build(ROOT)

    unlabeled = [n for n, labels in result.graph.nodes(data="labels") if not labels]
    assert len(unlabeled) == 3
    assert SANCTIONED in result.graph
    assert result.stats.node_limit_reached


async def test_transfers_seen_from_both_ends_are_counted_once() -> None:
    lookup = FakeLookup([tx(ROOT, a(2)), tx(ROOT, a(2)), tx(a(2), ROOT, value=5)])
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    assert result.graph.edges[ROOT, a(2)]["tx_count"] == 2
    assert result.graph.edges[ROOT, a(2)]["total_value_wei"] == 2 * 10**18
    assert result.graph.edges[a(2), ROOT]["tx_count"] == 1
    assert a(2) in lookup.expanded()


async def test_token_transfers_count_separately_from_eth_value() -> None:
    lookup = FakeLookup(
        [tx(ROOT, a(2), value=7, endpoint=Endpoint.TOKEN), tx(ROOT, a(2), ts=1_800_000_000)]
    )
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    edge = result.graph.edges[ROOT, a(2)]
    assert edge["tx_count"] == 2
    assert edge["token_transfer_count"] == 1
    assert edge["total_value_wei"] == 10**18
    assert edge["last_seen"] == 1_800_000_000


async def test_stats_count_lookups_and_api_calls() -> None:
    lookup = FakeLookup([tx(ROOT, a(2))])
    result = await GraphBuilder(lookup, LABELS, params()).build(ROOT)

    # Root and a(2): three endpoints each.
    assert result.stats.lookups == 6
    assert result.stats.api_calls == 6
    assert result.stats.expanded == 2


async def test_rejects_invalid_root_address() -> None:
    with pytest.raises(ValueError):
        await GraphBuilder(FakeLookup([]), LABELS, params()).build("0x123")
