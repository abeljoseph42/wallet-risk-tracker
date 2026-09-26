"""Evaluate the scorer on a versioned eval set and write docs/EVALUATION.md.

For each label mode (hold-out: the scorer sees only the scoring half of the flagged
labels; in-sample: it sees all of them), every wallet's graph is built once at the eval
set's snapshot and scored under four parameter variants: the full model, hop weighting
off (hop_decay = 1), exchange handling off, and both off (baseline). Every number in
the report is computed here.

Usage: docker compose run --rm backend python scripts/run_eval.py [--version v1]
Reruns are served from cache (no API calls once the snapshot is covered).
"""

import argparse
import asyncio
import csv
import datetime
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.clients.etherscan import EtherscanError, client_from_settings
from app.config import ScoringParams, get_settings, load_scoring_params
from app.db.session import get_engine, get_sessionmaker
from app.services.cache import Snapshot, TransactionCache
from app.services.evaluation import (
    Confusion,
    EvalSet,
    Example,
    average_precision,
    confusion,
    pr_curve,
    wilson_interval,
)
from app.services.graph import GraphBuilder
from app.services.scoring import score_graph
from app.services.valuation import make_valuer

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO / "data" / "eval"
REPORT = REPO / "docs" / "EVALUATION.md"
MODES = ("holdout", "in_sample")
PR_THRESHOLDS = [float(t) for t in range(5, 100, 5)]


def variants(params: ScoringParams) -> dict[str, ScoringParams]:
    no_hop = params.model_copy(update={"hop_decay": 1.0})
    off = params.exchange_handling.model_copy(update={"enabled": False})
    return {
        "full": params,
        "no_hop_weighting": no_hop,
        "no_exchange_handling": params.model_copy(update={"exchange_handling": off}),
        "baseline": no_hop.model_copy(update={"exchange_handling": off}),
    }


@dataclass
class Scored:
    example: Example
    scores: dict[str, float]


def label_index(eval_set: EvalSet, mode: str) -> dict[str, frozenset[str]]:
    flagged = eval_set.scoring_flagged if mode == "holdout" else list(eval_set.flagged_labels)
    index: dict[str, set[str]] = {a: {"exchange"} for a in eval_set.exchange_labels}
    for address in flagged:
        index.setdefault(address, set()).add(eval_set.flagged_labels[address])
    return {a: frozenset(t) for a, t in index.items()}


async def score_all(
    eval_set: EvalSet, params: ScoringParams
) -> tuple[dict[str, list[Scored]], set[str], int]:
    settings = get_settings()
    sessions = get_sessionmaker()
    snapshot = Snapshot(
        eval_set.snapshot_block, datetime.datetime.fromisoformat(eval_set.snapshot_time)
    )
    param_sets = variants(params)
    results: dict[str, list[Scored]] = {mode: [] for mode in MODES}
    failed: set[str] = set()
    api_calls = 0
    async with httpx.AsyncClient(timeout=30) as http:
        cache = TransactionCache(
            sessions, client_from_settings(settings, http), ttl_seconds=settings.cache_ttl_seconds
        )
        for mode in MODES:
            builder = GraphBuilder(
                cache,
                label_index(eval_set, mode),
                params.graph,
                make_valuer(params.valuation),
                snapshot=snapshot,
            )
            started = time.perf_counter()
            for i, example in enumerate(eval_set.examples, start=1):
                try:
                    graph = await builder.build(example.address)
                except EtherscanError as exc:
                    print(f"  {mode}: {example.address} failed: {exc.kind}: {exc}")
                    failed.add(example.address)
                    continue
                api_calls += graph.stats.api_calls
                scores = {name: score_graph(graph, p).score for name, p in param_sets.items()}
                results[mode].append(Scored(example, scores))
                if i % 25 == 0 or i == len(eval_set.examples):
                    print(
                        f"  {mode}: {i}/{len(eval_set.examples)} wallets, "
                        f"{api_calls} API calls, {time.perf_counter() - started:.0f}s"
                    )
    await get_engine().dispose()
    # Compare modes and variants on the same wallets.
    for mode in MODES:
        results[mode] = [r for r in results[mode] if r.example.address not in failed]
    return results, failed, api_calls


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _ci(k: int, n: int) -> str:
    lo, hi = wilson_interval(k, n)
    return f"{_pct(k / n) if n else 'n/a'} [{_pct(lo)}, {_pct(hi)}]"


def _metrics_row(name: str, scored: list[Scored], variant: str, threshold: float) -> str:
    scores = [r.scores[variant] for r in scored]
    labels = [r.example.label for r in scored]
    c = confusion(scores, labels, threshold)
    return (
        f"| {name} | {_ci(c.tp, c.tp + c.fp)} | {_ci(c.tp, c.tp + c.fn)} | {c.f1:.3f} "
        f"| {average_precision(scores, labels):.3f} | {c.tp} | {c.fp} | {c.fn} |"
    )


def _group_confusion(scored: list[Scored], variant: str, group: str, threshold: float) -> Confusion:
    subset = [r for r in scored if r.example.group == group]
    return confusion(
        [r.scores[variant] for r in subset], [r.example.label for r in subset], threshold
    )


def render(
    eval_set: EvalSet,
    params: ScoringParams,
    results: dict[str, list[Scored]],
    failed: set[str],
    api_calls: int,
) -> str:
    threshold = params.buckets.high
    param_sets = variants(params)
    holdout = results["holdout"]
    counts: dict[str, int] = defaultdict(int)
    for r in holdout:
        counts[r.example.group] += 1
    n_pos = sum(r.example.label for r in holdout)
    n_neg = len(holdout) - n_pos

    def fp(variant: str, group: str | None = None) -> int:
        rows = [r for r in holdout if r.example.label == 0 and group in (None, r.example.group)]
        return sum(r.scores[variant] >= threshold for r in rows)

    base_fp, full_fp = fp("baseline"), fp("full")
    reduction = (base_fp - full_fp) / base_fp if base_fp else 0.0
    full = confusion(
        [r.scores["full"] for r in holdout], [r.example.label for r in holdout], threshold
    )

    lines = [
        "# Evaluation",
        "",
        "Generated by `backend/scripts/run_eval.py`; do not edit by hand. Reproduce with:",
        "",
        "```bash",
        f"docker compose run --rm backend python scripts/run_eval.py --version {eval_set.version}",
        "```",
        "",
        "## Headline (hold-out labels, full model)",
        "",
        f"A wallet is flagged when its score is ≥ {threshold:g} (the High bucket). "
        f"N = {len(holdout)} wallets ({n_pos} positive, {n_neg} negative).",
        "",
        f"- **Precision {_ci(full.tp, full.tp + full.fp)}**, "
        f"**recall {_ci(full.tp, full.tp + full.fn)}** (95% Wilson intervals), "
        f"F1 {full.f1:.3f}.",
        f"- **Hop weighting + exchange handling cut false positives by {_pct(reduction)}** "
        f"({base_fp} → {full_fp} of {n_neg} negatives) versus the same scorer with both off.",
        f"- Exchange-wallet false-positive rate: "
        f"{_ci(fp('full', 'neg_exchange'), counts['neg_exchange'])} "
        f"(baseline {_ci(fp('baseline', 'neg_exchange'), counts['neg_exchange'])}).",
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
        "- Groups: "
        + ", ".join(
            f"{g} {counts[g]}"
            for g in ("pos_sanctioned", "pos_mixer", "neg_random", "neg_exchange")
        )
        + ".",
        f"- Random negatives dropped for transacting with a held-out address: "
        f"{eval_set.excluded_contaminated}. Wallets that failed to score (excluded from every "
        f"row): {len(failed)}.",
        f"- Etherscan calls made by this run: {api_calls}.",
        "- Params hashes: "
        + ", ".join(f"{name} `{p.params_hash}`" for name, p in param_sets.items())
        + ".",
        "",
        f"## Results at threshold {threshold:g}",
        "",
        "Precision and recall with 95% Wilson intervals; AP = average precision over all "
        "thresholds.",
        "",
    ]
    for mode in MODES:
        title = (
            "Hold-out labels (the honest setting)"
            if mode == "holdout"
            else "In-sample labels (CLAUDE.md as written: positives are 1 hop from labels the "
            "scorer sees, so recall is close to circular)"
        )
        lines += [
            f"### {title}",
            "",
            "| Variant | Precision | Recall | F1 | AP | TP | FP | FN |",
            "|---|---|---|---|---|---|---|---|",
        ]
        lines += [_metrics_row(name, results[mode], name, threshold) for name in param_sets]
        lines.append("")

    lines += [
        "### By group (hold-out labels)",
        "",
        "| Variant | Recall: sanctioned | Recall: mixer | FPR: random | FPR: exchange |",
        "|---|---|---|---|---|",
    ]
    for name in param_sets:
        cells = []
        for group in ("pos_sanctioned", "pos_mixer"):
            c = _group_confusion(holdout, name, group, threshold)
            cells.append(_ci(c.tp, c.tp + c.fn))
        for group in ("neg_random", "neg_exchange"):
            c = _group_confusion(holdout, name, group, threshold)
            cells.append(_ci(c.fp, c.fp + c.tn))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Precision-recall curve (hold-out labels)",
        "",
        "| Threshold | Precision (full) | Recall (full) "
        "| Precision (baseline) | Recall (baseline) |",
        "|---|---|---|---|---|",
    ]
    scores_full = [r.scores["full"] for r in holdout]
    scores_base = [r.scores["baseline"] for r in holdout]
    labels = [r.example.label for r in holdout]
    for (t, p_full, r_full), (_, p_base, r_base) in zip(
        pr_curve(scores_full, labels, PR_THRESHOLDS),
        pr_curve(scores_base, labels, PR_THRESHOLDS),
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
        "- **In-sample** numbers show how much easier the circular setup is. The gap between "
        "the two is the leakage the hold-out design removes.",
        "- **Random negatives are unlabeled, not verified clean.** Some may be genuinely "
        "risky, which would make measured precision a lower bound.",
        "- The threshold (High bucket) was fixed before looking at results; the PR curve "
        "shows the trade-off at every other threshold.",
        "",
    ]
    return "\n".join(lines)


def write_csv(path: Path, results: dict[str, list[Scored]], names: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "address", "group", "label", *names])
        for mode in MODES:
            for r in results[mode]:
                writer.writerow(
                    [mode, r.example.address, r.example.group, r.example.label]
                    + [r.scores[n] for n in names]
                )


async def main(version: str) -> None:
    eval_set = EvalSet.from_json((EVAL_DIR / f"eval_set_{version}.json").read_text())
    params = load_scoring_params()
    print(f"Evaluating {len(eval_set.examples)} wallets at block {eval_set.snapshot_block}")
    results, failed, api_calls = await score_all(eval_set, params)
    write_csv(EVAL_DIR / f"results_{version}.csv", results, list(variants(params)))
    REPORT.write_text(render(eval_set, params, results, failed, api_calls))
    print(f"Wrote {REPORT.relative_to(REPO)} and data/eval/results_{version}.csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1")
    asyncio.run(main(parser.parse_args().version))
