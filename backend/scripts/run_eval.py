"""Evaluate the scorer on a versioned eval set and write a Markdown report.

For each label mode (hold-out: the scorer sees only the scoring half of the flagged
labels; in-sample: it sees all of them), every wallet's graph is built once at the eval
set's snapshot and scored under the full model and scoring ablations. With --naive, a
third set of graphs is built that traverses through exchanges and hubs and is scored with
every discount off: the naive baseline for the false-positive comparison. Every number
in the report is computed here.

Usage: docker compose run --rm backend python scripts/run_eval.py \\
           [--version v2] [--report docs/EVALUATION.md] [--naive]
"""

import argparse
import asyncio
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.config import ScoringParams, load_scoring_params
from app.db.session import get_engine
from app.services.eval_runner import (
    MODES,
    build_graphs,
    label_index,
    naive_traversal,
    scoring_variants,
)
from app.services.evaluation import (
    Confusion,
    EvalSet,
    Example,
    average_precision,
    confusion,
    pr_curve,
    wilson_interval,
)
from app.services.scoring import score_graph

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO / "data" / "eval"
PR_THRESHOLDS = [1.0, 2.0, *(float(t) for t in range(5, 100, 5))]
NAIVE = "naive_traversal"
DESCRIPTIONS = {
    "full": "The production model.",
    "no_hop_weighting": "hop_decay = 1: a 3-hop link counts as much as a direct one.",
    "no_flow_factor": "flow_factor = 1: dust counts as much as moving all funds.",
    "no_exchange_handling": "No discount for paths through exchanges or hubs.",
    "naive_scoring": "All three scoring discounts off (same graphs as full).",
    NAIVE: "Naive baseline: traverses through exchanges and hubs, all discounts off.",
}


@dataclass
class Scored:
    example: Example
    scores: dict[str, float]


async def score_all(
    eval_set: EvalSet, params: ScoringParams, include_naive: bool
) -> tuple[dict[str, list[Scored]], set[str], int]:
    variants = scoring_variants(params)
    naive_graph, naive_params = naive_traversal(params)
    by_mode: dict[str, dict[str, dict[str, float]]] = {mode: {} for mode in MODES}
    failed: set[str] = set()
    api_calls = 0

    for mode in MODES:
        run = await build_graphs(
            eval_set, label_index(eval_set, mode), params.graph, params, name=mode
        )
        failed |= run.failed
        api_calls += run.api_calls
        for address, graph in run.graphs.items():
            by_mode[mode][address] = {n: score_graph(graph, p).score for n, p in variants.items()}

    if include_naive:
        run = await build_graphs(
            eval_set, label_index(eval_set, "holdout"), naive_graph, params, name=NAIVE
        )
        failed |= run.failed
        api_calls += run.api_calls
        for address, graph in run.graphs.items():
            if address in by_mode["holdout"]:
                by_mode["holdout"][address][NAIVE] = score_graph(graph, naive_params).score
    await get_engine().dispose()

    results = {
        mode: [
            Scored(e, by_mode[mode][e.address])
            for e in eval_set.examples
            if e.address not in failed and e.address in by_mode[mode]
        ]
        for mode in MODES
    }
    return results, failed, api_calls


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _ci(k: int, n: int) -> str:
    lo, hi = wilson_interval(k, n)
    return f"{_pct(k / n) if n else 'n/a'} [{_pct(lo)}, {_pct(hi)}]"


def _confusion(scored: list[Scored], variant: str, threshold: float) -> Confusion:
    return confusion(
        [r.scores[variant] for r in scored], [r.example.label for r in scored], threshold
    )


def _metrics_row(scored: list[Scored], variant: str, threshold: float) -> str:
    c = _confusion(scored, variant, threshold)
    ap = average_precision([r.scores[variant] for r in scored], [r.example.label for r in scored])
    return (
        f"| {variant} | {_ci(c.tp, c.tp + c.fp)} | {_ci(c.tp, c.tp + c.fn)} | {c.f1:.3f} "
        f"| {ap:.3f} | {c.tp} | {c.fp} | {c.fn} |"
    )


def render(
    eval_set: EvalSet,
    params: ScoringParams,
    results: dict[str, list[Scored]],
    failed: set[str],
    api_calls: int,
    *,
    report_name: str,
) -> str:
    threshold = params.flag_threshold
    holdout = results["holdout"]
    variants = [v for v in DESCRIPTIONS if holdout and v in holdout[0].scores]
    baseline = NAIVE if NAIVE in variants else "naive_scoring"
    counts: dict[str, int] = defaultdict(int)
    for r in holdout:
        counts[r.example.group] += 1
    positives = [r for r in holdout if r.example.label == 1]
    negatives = [r for r in holdout if r.example.label == 0]

    def fp(variant: str, group: str | None = None) -> int:
        rows = [r for r in negatives if group in (None, r.example.group)]
        return sum(r.scores[variant] >= threshold for r in rows)

    def signal(rows: list[Scored], variant: str) -> str:
        return _ci(sum(r.scores[variant] > 0 for r in rows), len(rows))

    full = _confusion(holdout, "full", threshold)
    base_fp, full_fp = fp(baseline), fp("full")
    reduction = (base_fp - full_fp) / base_fp if base_fp else 0.0
    n_ex = counts["neg_exchange"]
    groups = ("pos_sanctioned", "pos_mixer", "neg_random", "neg_exchange")
    lines = [
        "# Evaluation",
        "",
        "Generated by `backend/scripts/run_eval.py`; do not edit by hand. Reproduce with:",
        "",
        "```bash",
        "docker compose run --rm backend python scripts/run_eval.py "
        f"--version {eval_set.version} --report {report_name}"
        + (" --naive" if NAIVE in variants else ""),
        "```",
        "",
        "## Headline (hold-out labels, full model)",
        "",
        f"A wallet is flagged when its score is ≥ {threshold:g} (`flag_threshold`). "
        f"N = {len(holdout)} wallets ({len(positives)} positive, {len(negatives)} negative).",
        "",
        f"- **Precision {_ci(full.tp, full.tp + full.fp)}**, "
        f"**recall {_ci(full.tp, full.tp + full.fn)}** (95% Wilson intervals), "
        f"F1 {full.f1:.3f}.",
        f"- **False positives: {base_fp} → {full_fp} of {len(negatives)} negatives "
        f"({_pct(reduction)} fewer)** than the `{baseline}` baseline: "
        f"{DESCRIPTIONS[baseline]}",
        f"- Exchange-wallet false-positive rate: {_ci(fp('full', 'neg_exchange'), n_ex)} "
        f"(`{baseline}`: {_ci(fp(baseline, 'neg_exchange'), n_ex)}).",
        f"- Signal coverage (score > 0): positives {signal(positives, 'full')}, "
        f"negatives {signal(negatives, 'full')}.",
        "",
        "## Setup",
        "",
        f"- Eval set `data/eval/eval_set_{eval_set.version}.json`, built "
        f"{eval_set.created_at}, seed {eval_set.seed}.",
        f"- Snapshot: block {eval_set.snapshot_block} ({eval_set.snapshot_time}). Only history "
        "up to this block is used.",
        f"- Labels: {len(eval_set.flagged_labels)} flagged addresses split into "
        f"{len(eval_set.scoring_flagged)} scoring / {len(eval_set.heldout_flagged)} held-out "
        f"(by SDN entity or contract), plus {len(eval_set.exchange_labels)} exchange labels. "
        f"Sources: {', '.join(f'{k} ({v})' for k, v in eval_set.label_sources.items())}.",
        "- Groups: " + ", ".join(f"{g} {counts[g]}" for g in groups) + ".",
        f"- Random negatives dropped for transacting with a held-out address: "
        f"{eval_set.excluded_contaminated}. Wallets that failed to score (excluded from every "
        f"row): {len(failed)}.",
        f"- Etherscan calls made by this run: {api_calls}.",
        f"- Full-model params hash: `{params.params_hash}`.",
        "",
        "| Variant | What changes |",
        "|---|---|",
        *(f"| {v} | {DESCRIPTIONS[v]} |" for v in variants),
        "",
        f"## Results at threshold {threshold:g}",
        "",
        "Precision and recall with 95% Wilson intervals. AP = average precision over all "
        "thresholds; wallets tied at a score of 0 form one step, which inflates AP when "
        "many wallets have no signal at all, so read it together with signal coverage.",
        "",
    ]
    for mode in MODES:
        title = (
            "Hold-out labels (the setting to quote)"
            if mode == "holdout"
            else "In-sample labels (CLAUDE.md as written: positives are 1 hop from labels the "
            "scorer sees, so recall is close to circular)"
        )
        mode_variants = [v for v in variants if results[mode] and v in results[mode][0].scores]
        lines += [
            f"### {title}",
            "",
            "| Variant | Precision | Recall | F1 | AP | TP | FP | FN |",
            "|---|---|---|---|---|---|---|---|",
            *(_metrics_row(results[mode], v, threshold) for v in mode_variants),
            "",
        ]

    lines += [
        "### By group (hold-out labels)",
        "",
        "| Variant | Recall: sanctioned | Recall: mixer | FPR: random | FPR: exchange |",
        "|---|---|---|---|---|",
    ]
    for variant in variants:
        cells = []
        for group in groups:
            c = _confusion([r for r in holdout if r.example.group == group], variant, threshold)
            positive = group.startswith("pos")
            cells.append(_ci(c.tp, c.tp + c.fn) if positive else _ci(c.fp, c.fp + c.tn))
        lines.append(f"| {variant} | " + " | ".join(cells) + " |")

    labels = [r.example.label for r in holdout]
    lines += [
        "",
        "## Precision-recall curve (hold-out labels)",
        "",
        f"| Threshold | Precision (full) | Recall (full) | Precision ({baseline}) "
        f"| Recall ({baseline}) |",
        "|---|---|---|---|---|",
    ]
    for (t, p_full, r_full), (_, p_base, r_base) in zip(
        pr_curve([r.scores["full"] for r in holdout], labels, PR_THRESHOLDS),
        pr_curve([r.scores[baseline] for r in holdout], labels, PR_THRESHOLDS),
        strict=True,
    ):
        lines.append(
            f"| {t:g} | {_pct(p_full)} | {_pct(r_full)} | {_pct(p_base)} | {_pct(r_base)} |"
        )

    lines += [
        "",
        "## How to read this",
        "",
        "- **Hold-out** numbers are the ones to quote. Positives were chosen by transacting "
        "with addresses the scorer never sees, so detecting them means the graph found "
        "*other* known-bad addresses nearby.",
        "- **In-sample** numbers show how much easier the circular setup is; the gap between "
        "the two is the leakage the hold-out design removes.",
        "- **Random negatives are unlabeled, not verified clean.** Some may be genuinely "
        "risky, which would make measured precision a lower bound.",
        f"- The threshold ({threshold:g}) was fixed before this set was scored; the PR curve "
        "shows every other operating point.",
        "",
    ]
    return "\n".join(lines)


def write_csv(path: Path, results: dict[str, list[Scored]]) -> None:
    variants = sorted({v for rows in results.values() for r in rows for v in r.scores})
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "address", "group", "label", *variants])
        for mode in MODES:
            for r in results[mode]:
                writer.writerow(
                    [mode, r.example.address, r.example.group, r.example.label]
                    + [r.scores.get(v, "") for v in variants]
                )


async def main(version: str, report: str, include_naive: bool) -> None:
    eval_set = EvalSet.from_json((EVAL_DIR / f"eval_set_{version}.json").read_text())
    params = load_scoring_params()
    print(f"Evaluating {len(eval_set.examples)} wallets at block {eval_set.snapshot_block}")
    results, failed, api_calls = await score_all(eval_set, params, include_naive)
    write_csv(EVAL_DIR / f"results_{version}.csv", results)
    (REPO / report).write_text(
        render(eval_set, params, results, failed, api_calls, report_name=report)
    )
    print(f"Wrote {report} and data/eval/results_{version}.csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v2")
    parser.add_argument("--report", default="docs/EVALUATION.md")
    parser.add_argument("--naive", action="store_true", help="also build naive-traversal graphs")
    args = parser.parse_args()
    asyncio.run(main(args.version, args.report, args.naive))
