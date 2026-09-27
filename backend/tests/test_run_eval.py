"""Tests for eval label modes, variants, and the numbers the report computes."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from app.config import load_scoring_params
from app.services.eval_runner import label_index, naive_traversal, scoring_variants
from app.services.evaluation import EvalSet, Example, Group


def _load_run_eval() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = module
    spec.loader.exec_module(module)
    return module


run_eval = _load_run_eval()


def _a(n: int) -> str:
    return f"0x{n:040x}"


def _eval_set(examples: list[Example]) -> EvalSet:
    return EvalSet(
        version="test",
        created_at="2026-09-26T00:00:00+00:00",
        seed=1,
        snapshot_block=100,
        snapshot_time="2026-09-26T00:00:00+00:00",
        label_sources={"ofac_sdn": 2, "seed": 2},
        flagged_labels={_a(1): "sanctioned", _a(2): "mixer"},
        scoring_flagged=[_a(1)],
        heldout_flagged=[_a(2)],
        exchange_labels=[_a(9)],
        examples=examples,
    )


def test_label_index_hides_heldout_labels_only_in_holdout_mode() -> None:
    eval_set = _eval_set([])

    holdout = label_index(eval_set, "holdout")
    in_sample = label_index(eval_set, "in_sample")

    assert _a(2) not in holdout
    assert in_sample[_a(2)] == frozenset({"mixer"})
    assert holdout[_a(1)] == frozenset({"sanctioned"})
    assert holdout[_a(9)] == in_sample[_a(9)] == frozenset({"exchange"})


def test_scoring_variants_switch_exactly_one_discount_each() -> None:
    params = load_scoring_params()

    v = scoring_variants(params)

    assert v["full"] is params
    assert v["no_hop_weighting"].hop_decay == 1.0
    assert not v["no_flow_factor"].flow.enabled
    assert not v["no_exchange_handling"].exchange_handling.enabled
    naive = v["naive_scoring"]
    assert naive.hop_decay == 1.0 and not naive.flow.enabled
    assert not naive.exchange_handling.enabled
    assert len({p.params_hash for p in v.values()}) == 5


def test_naive_traversal_expands_exchanges_and_hubs() -> None:
    graph, naive = naive_traversal(load_scoring_params())

    assert graph.expand_exchanges and graph.expand_hubs
    assert naive.graph == graph
    assert not naive.flow.enabled


def _scored(address: int, label: int, group: Group, full: float, naive: float) -> Any:
    scores = {name: full for name in ("full", "no_hop_weighting", "no_flow_factor")}
    scores |= {"no_exchange_handling": full, "naive_scoring": full, "naive_traversal": naive}
    return run_eval.Scored(Example(_a(address), label, group, "test"), scores)


def _render(rows: list[Any], *, tuned: bool = False) -> str:
    params = load_scoring_params().model_copy(update={"flag_threshold": 50})
    eval_set = _eval_set([r.example for r in rows])
    report: str = run_eval.render(
        eval_set,
        params,
        {"holdout": rows, "in_sample": rows},
        set(),
        7,
        report_name="docs/EVALUATION.md",
        tuned_on_this_set=tuned,
    )
    return report


def test_render_reports_headline_metrics_and_fp_reduction_vs_naive() -> None:
    rows = [
        _scored(10, 1, "pos_sanctioned", full=80, naive=90),  # TP in both
        _scored(11, 1, "pos_mixer", full=30, naive=70),  # FN full, TP naive
        _scored(20, 0, "neg_random", full=0, naive=70),  # FP only in naive
        _scored(21, 0, "neg_exchange", full=55, naive=75),  # FP in both
    ]

    report = _render(rows)

    assert "N = 4 wallets (2 positive, 2 negative)" in report
    # Full model: TP 1, FP 1, FN 1 -> precision 50%, recall 50%.
    assert "**Precision 50.0%" in report
    assert "**recall 50.0%" in report
    assert "**False positives: 2 → 1 of 2 negatives (50.0% fewer)** than the `naive_traversal`" in (
        report
    )
    # Signal coverage: both positives > 0; one of two negatives > 0.
    assert "positives 100.0%" in report
    assert "negatives 50.0%" in report
    assert "Etherscan calls made by this run: 7" in report
    assert "--naive" in report


def test_render_has_a_results_table_per_mode() -> None:
    rows = [_scored(10, 1, "pos_mixer", 80, 80), _scored(20, 0, "neg_random", 0, 0)]

    report = _render(rows)

    assert "Hold-out labels (the setting to quote)" in report
    assert "In-sample labels" in report
    assert report.count("| Variant | Precision | Recall | F1 | AP | TP | FP | FN |") == 2


def test_tuned_on_this_set_labels_the_numbers_as_optimistic() -> None:
    rows = [_scored(10, 1, "pos_mixer", 80, 80), _scored(20, 0, "neg_random", 0, 0)]

    tuned = _render(rows, tuned=True)
    untuned = _render(rows)

    assert "**Tuned on this set.**" in tuned
    assert "docs/evaluation/tuning_test.md" in tuned
    assert "docs/evaluation/test_dev.md" in tuned
    assert "--tuned-on-this-set" in tuned
    assert "was fixed before this set was scored" not in tuned
    assert "Tuned on this set" not in untuned
    assert "was fixed before this set was scored" in untuned
