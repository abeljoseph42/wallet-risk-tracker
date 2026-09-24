"""Tests for loading tunable parameters from config/*.yaml."""

from pathlib import Path

import pydantic
import pytest

from app.config import load_graph_params


def test_repo_scoring_yaml_loads() -> None:
    params = load_graph_params()

    assert params.max_hops == 3
    assert params.max_expanded_nodes == 100


def test_unknown_graph_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "scoring.yaml"
    path.write_text("max_hops: 3\ngraph:\n  max_expanded_nodes: 1\n  typo_key: 5\n")

    with pytest.raises(pydantic.ValidationError, match="typo_key"):
        load_graph_params(path)
