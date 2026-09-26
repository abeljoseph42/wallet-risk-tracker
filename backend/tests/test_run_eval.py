"""Tests for the evaluation report: label modes and the numbers render() computes."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.config import load_scoring_params
from app.services.evaluation import EvalSet, Example


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

    holdout = run_eval.label_index(eval_set, "holdout")
    in_sample = run_eval.label_index(eval_set, "in_sample")

    assert _a(2) not in holdout
    assert in_sample[_a(2)] == frozenset({"mixer"})
    assert holdout[_a(1)] == frozenset({"sanctioned"})
    assert holdout[_a(9)] == in_sample[_a(9)] == frozenset({"exchange"})


def test_variants_differ_only_in_the_switched_parameters() -> None:
    params = load_scoring_params()

    v = run_eval.variants(params)

    assert v["full"] is params
    assert v["no_hop_weighting"].hop_decay == 1.0
    assert not v["no_exchange_handling"].exchange_handling.enabled
    assert v["baseline"].hop_decay == 1.0 and not v["baseline"].exchange_handling.enabled
    assert len({p.params_hash for p in v.values()}) == 4


def _scored(address: int, label: int, group: str, full: float, base: float) -> object:
    example = Example(_a(address), label, group, "test")  # type: ignore[arg-type]
    scores = {
        "full": full,
        "no_hop_weighting": full,
        "no_exchange_handling": full,
        "baseline": base,
    }
    return run_eval.Scored(example, scores)


def test_render_reports_headline_metrics_and_fp_reduction() -> None:
    rows = [
        _scored(10, 1, "pos_sanctioned", full=80, base=90),  # TP in both
        _scored(11, 1, "pos_mixer", full=30, base=60),  # FN full, TP baseline
        _scored(20, 0, "neg_random", full=10, base=70),  # FP only in baseline
        _scored(21, 0, "neg_exchange", full=55, base=75),  # FP in both
    ]
    eval_set = _eval_set([r.example for r in rows])  # type: ignore[attr-defined]
    params = load_scoring_params()

    report = run_eval.render(
        eval_set, params, {"holdout": rows, "in_sample": rows}, set(), api_calls=7
    )

    assert "N = 4 wallets (2 positive, 2 negative)" in report
    # Full model: TP 1, FP 1, FN 1 -> precision 50%, recall 50%.
    assert "**Precision 50.0%" in report
    assert "**recall 50.0%" in report
    # False positives fall from 2 (baseline) to 1 (full).
    assert "cut false positives by 50.0%** (2 → 1 of 2 negatives)" in report
    assert "Etherscan calls made by this run: 7" in report
    assert params.params_hash in report


@pytest.mark.parametrize("mode", ["holdout", "in_sample"])
def test_render_has_a_results_table_per_mode(mode: str) -> None:
    rows = [_scored(10, 1, "pos_mixer", 80, 80), _scored(20, 0, "neg_random", 10, 10)]
    eval_set = _eval_set([r.example for r in rows])  # type: ignore[attr-defined]

    report = run_eval.render(
        eval_set, load_scoring_params(), {"holdout": rows, "in_sample": rows}, set(), 0
    )

    heading = "Hold-out labels" if mode == "holdout" else "In-sample labels"
    assert heading in report
    assert report.count("| Variant | Precision | Recall | F1 | AP | TP | FP | FN |") == 2
