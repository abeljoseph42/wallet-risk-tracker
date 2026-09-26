# Scoring

A wallet's risk score (0–100) measures how close its transaction graph gets to flagged
addresses (sanctioned, malicious or mixer), weighted by distance and by how much value
actually moved. Code: `backend/app/services/scoring.py`. Parameters:
`backend/config/scoring.yaml`. Every score reports a `params_hash` of the parameters used.

## Formula

For each flagged address *f* found within `max_hops` of the target:

```
c_f   = severity(f) * hop_decay^(d-1) * flow_factor * discount
score = 100 * (1 - prod over f of (1 - c_f))
```

Treating each `c_f` as an independent chance that the link is risky, the score is the
chance that at least one is. So several weak signals add up, but the score can't pass
100, and one strong signal dominates.

### Terms

| Term | Definition | Default |
|---|---|---|
| `severity(f)` | By label: sanctioned 1.0, malicious 0.8, mixer 0.7. An address with several risk labels uses the highest. A per-address `severity` in `address_labels` overrides the default. | see table |
| `d` | Hop count of the chosen path, ignoring edge direction. A wallet that *received* from a mixer is as close to it as one that sent to it. `d = 0` when the target itself is flagged. | `max_hops: 3` |
| `hop_decay^(d-1)` | 1 hop = 1.0, 2 hops = 0.5, 3 hops = 0.25. `d = 0` also gets 1.0. | `hop_decay: 0.5` |
| `flow_factor` | `min(1, share / share_saturation)`. See below. | `share_saturation: 0.10` |
| `discount` | 1 for a path that avoids exchanges and hubs. For a path through one: `exchange_discount` in "discount" mode, 0 in "cut" mode. Always 1 when `exchange_handling.enabled` is false. | `exchange_discount: 0.2` |

**Buckets** (inclusive lower bounds): Low 0, Medium 25, High 50, Severe 75.

### Flow factor

```
bottleneck = min over consecutive (u, v) on the path of value(u->v) + value(v->u)
share      = bottleneck / (target's total in+out volume)
flow       = min(1, share / share_saturation)
```

- **Bottleneck:** a path is only as strong as its weakest hop. If the target sent a
  neighbor 1 ETH, and that neighbor sent 1,000 ETH to a mixer, the target's exposure is
  bounded by the 1 ETH.
- **Share of the target's own volume:** 800 ETH to a mixer is a lot for a 1,000 ETH
  wallet and very little for a 175,000 ETH one. The denominator counts *all* of the
  target's kept transfers, including counterparties left out of the graph.
- **Saturation:** without it, a wallet that moved 20% of its funds through a mixer would
  get only 0.2 weight. At the default 0.10, anything at or above 10% of volume gets full
  weight, 1% gets 0.1, and dust gets about 0.
- **Values** are in ETH-equivalent wei. ETH counts at face value (normal and internal
  transfers). USDT, USDC and DAI count at $1, converted at a fixed `usd_per_eth` (2,685.59,
  Etherscan `stats/ethprice` on 2026-09-25) so that results are reproducible. Every other
  token counts as 0: without prices its units are meaningless, and it keeps spam-token
  airdrops from creating value-weighted links.

### Choosing the path

A flagged address can be reached by several paths. The candidates are:
1. the shortest paths in the whole graph, and
2. the shortest paths that avoid exchanges and hubs (hubs are unlabeled addresses the
   graph builder stopped at for being too busy).

Up to 50 paths of each kind are scored, and the one with the **largest contribution** is
reported. So the exchange discount never penalizes a wallet that also has an equally
short clean path. A longer clean path can also beat a shorter discounted one:
3 hops clean (0.25) beats 2 hops via an exchange (0.5 × 0.2 = 0.1).

## Worked examples

From `tests/test_scoring.py` (1 ETH edges, full flow unless stated):

| Case | Contribution | Score |
|---|---|---|
| Target is sanctioned | 1.0 | 100 (Severe) |
| Sanctioned at 1 hop | 1.0 · 1.0 · 1.0 | 100 |
| Sanctioned at 2 hops | 1.0 · 0.5 | 50 (High) |
| Sanctioned at 3 hops | 1.0 · 0.25 | 25 (Medium) |
| Mixer at 1 hop | 0.7 | 70 |
| Sanctioned at 2 hops via an exchange | 1.0 · 0.5 · 0.2 | 10 |
| Same, "cut" mode | 0 | 0 |
| Sanctioned at 1 hop, 5% of volume | 1.0 · 1.0 · 0.5 | 50 |
| Sanctioned 2 hops + mixer 2 hops | 0.5 and 0.35 | 100·(1 − 0.5·0.65) = 67.5 |
| 0.001 ETH from a sanctioned address, 100 ETH volume | < 0.001 | ≈ 0 |

Live, with default parameters (params `5beaee201cc7`, 2026-09-26):

| Wallet | Score | Driver |
|---|---|---|
| Tornado 1 ETH depositor A | 73.53 (High) | 60 ETH through the 10 ETH pool, 48% of its volume → full flow |
| Tornado 1 ETH depositor B | 74.67 (High) | same pattern, 48% of volume |
| vitalik.eth | 6.32 (Low) | 800 ETH through the Tornado proxy, but 0.46% of ~175k ETH volume |

These live runs are illustrations, not an evaluation. Precision and recall come from the
Phase 6 harness on a labeled set.

## Limitations

- **Graph proximity, not taint tracking.** The bottleneck compares aggregate amounts on
  each hop. It doesn't prove the *same* funds moved along the path, or that they moved in
  that order.
- **Fixed ETH price.** Stablecoin flows are converted at one rate regardless of when they
  happened. This only matters when a path mixes ETH and stablecoins.
- **Unpriced tokens count as zero.** Laundering purely through other tokens gets no flow
  weight until a price source exists (Phase 7).
- **Labels bound recall.** Mixer and exchange labels come from a 2023 snapshot, there are
  no `malicious` labels yet, and Tornado Cash is labeled `mixer`, not `sanctioned`,
  because OFAC delisted it in 2025.
- **The graph is bounded.** A flagged address behind the expansion budget, beyond the
  neighbor cap, or behind a hub isn't found (see `docs/ARCHITECTURE.md`, Phase 3). The
  score is a lower bound on proximity within those limits.
- **All defaults are starting points,** to be tuned against the Phase 6 evaluation set,
  with hop weighting and exchange handling measured on vs. off.
