"""Tests for the hold-out split, eval set format, and metrics."""

import pytest

from app.services.evaluation import (
    Confusion,
    EvalSet,
    Example,
    FlaggedLabel,
    average_precision,
    confusion,
    pr_curve,
    split_labels,
    wilson_interval,
)


def _a(n: int) -> str:
    return f"0x{n:040x}"


def test_group_key_keeps_an_sdn_entity_together() -> None:
    one = FlaggedLabel(_a(1), "sanctioned", "LAZARUS (SDN uid 42) [ETH]")
    two = FlaggedLabel(_a(2), "sanctioned", "LAZARUS (SDN uid 42) [USDT]")
    mixer = FlaggedLabel(_a(3), "mixer", "Tornado.Cash: 1 ETH")

    assert one.group_key == two.group_key == "sdn:42"
    assert mixer.group_key == f"addr:{_a(3)}"


def test_split_is_deterministic_disjoint_and_roughly_half_per_type() -> None:
    labels = [FlaggedLabel(_a(i), "sanctioned", f"E (SDN uid {i})") for i in range(20)]
    labels += [FlaggedLabel(_a(100 + i), "mixer") for i in range(10)]

    scoring, heldout = split_labels(labels, seed=7)

    assert (scoring, heldout) == split_labels(labels, seed=7)
    assert scoring.isdisjoint(heldout)
    assert scoring | heldout == {label.address for label in labels}
    assert len([a for a in heldout if int(a, 16) < 100]) == 10
    assert len([a for a in heldout if int(a, 16) >= 100]) == 5
    assert split_labels(labels, seed=8) != (scoring, heldout)


def test_split_never_separates_an_entitys_addresses() -> None:
    labels = [FlaggedLabel(_a(i), "sanctioned", f"E (SDN uid {i // 3})") for i in range(30)]

    scoring, heldout = split_labels(labels, seed=1)

    for entity in range(10):
        members = {_a(entity * 3 + k) for k in range(3)}
        assert members <= scoring or members <= heldout


def test_eval_set_round_trips_through_json() -> None:
    eval_set = EvalSet(
        version="v1",
        created_at="2026-09-26T00:00:00Z",
        seed=1,
        snapshot_block=100,
        snapshot_time="2026-09-26T00:00:00Z",
        label_sources={"ofac_sdn": 2},
        flagged_labels={_a(1): "sanctioned", _a(2): "mixer"},
        scoring_flagged=[_a(1)],
        heldout_flagged=[_a(2)],
        exchange_labels=[_a(9)],
        excluded_contaminated=1,
        examples=[Example(_a(3), 1, "pos_mixer", _a(2))],
    )

    assert EvalSet.from_json(eval_set.to_json()) == eval_set


def test_confusion_and_derived_metrics() -> None:
    c = confusion([90, 60, 40, 70, 10], [1, 1, 1, 0, 0], threshold=50)

    assert c == Confusion(tp=2, fp=1, tn=1, fn=1)
    assert c.precision == pytest.approx(2 / 3)
    assert c.recall == pytest.approx(2 / 3)
    assert c.f1 == pytest.approx(2 / 3)
    assert c.false_positive_rate == pytest.approx(0.5)


def test_metrics_handle_empty_denominators() -> None:
    c = confusion([10, 20], [0, 0], threshold=50)

    assert (c.precision, c.recall, c.f1) == (0.0, 0.0, 0.0)


def test_wilson_interval_matches_known_value_and_edges() -> None:
    lo, hi = wilson_interval(8, 10)

    assert lo == pytest.approx(0.4902, abs=1e-3)
    assert hi == pytest.approx(0.9433, abs=1e-3)
    assert wilson_interval(0, 0) == (0.0, 0.0)
    assert wilson_interval(10, 10)[1] == 1.0


def test_pr_curve_points() -> None:
    points = pr_curve([90, 60, 40], [1, 0, 1], thresholds=[50, 30])

    assert points == [(50, 0.5, 0.5), (30, pytest.approx(2 / 3), 1.0)]


def test_average_precision_perfect_and_worst_rankings() -> None:
    assert average_precision([90, 80, 10, 5], [1, 1, 0, 0]) == pytest.approx(1.0)
    # Both negatives rank first: recall steps at precision 1/3, then 1/2.
    assert average_precision([10, 5, 90, 80], [1, 1, 0, 0]) == pytest.approx(0.5 / 3 + 0.5 / 2)


def test_average_precision_treats_ties_as_one_step() -> None:
    assert average_precision([50, 50], [1, 0]) == average_precision([50, 50], [0, 1])
