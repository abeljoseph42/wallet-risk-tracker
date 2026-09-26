"""Tests for loading tunable parameters from config/*.yaml."""

from pathlib import Path

import pydantic
import pytest

from app.config import SCORING_CONFIG_PATH, load_graph_params, load_scoring_params


def test_repo_scoring_yaml_loads() -> None:
    params = load_graph_params()

    assert params.max_hops == 3
    assert params.max_expanded_nodes == 100


def test_unknown_graph_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "scoring.yaml"
    path.write_text("max_hops: 3\ngraph:\n  max_expanded_nodes: 1\n  typo_key: 5\n")

    with pytest.raises(pydantic.ValidationError, match="typo_key"):
        load_graph_params(path)


def test_scoring_params_load_with_nested_graph_params() -> None:
    params = load_scoring_params()

    assert params.graph == load_graph_params()
    assert params.severity.sanctioned == 1.0
    assert "0xdac17f958d2ee523a2206206994597c13d831ec7" in params.valuation.stablecoins


def test_params_hash_is_stable_and_changes_with_any_parameter() -> None:
    params = load_scoring_params()
    variant = params.model_copy(
        update={"exchange_handling": params.exchange_handling.model_copy(update={"enabled": False})}
    )

    assert params.params_hash == load_scoring_params().params_hash
    assert len(params.params_hash) == 12
    assert variant.params_hash != params.params_hash


def test_buckets_must_ascend(tmp_path: Path) -> None:
    text = SCORING_CONFIG_PATH.read_text().replace("high: 50", "high: 10")
    path = tmp_path / "scoring.yaml"
    path.write_text(text)

    with pytest.raises(pydantic.ValidationError, match="ascend"):
        load_scoring_params(path)
