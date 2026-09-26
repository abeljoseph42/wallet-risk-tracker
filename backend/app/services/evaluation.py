"""Evaluation set format, label hold-out split, and classification metrics.

Hold-out design (docs/EVALUATION.md): flagged labels are split into a *scoring* half, the
only labels the scorer sees, and a *held-out* half used only to pick positives (wallets
that transacted directly with a held-out address). Scoring with every label instead
("in-sample") would make recall nearly circular, since positives are defined by
proximity to the very labels the score measures proximity to.
"""

import json
import math
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal

Group = Literal["pos_sanctioned", "pos_mixer", "neg_random", "neg_exchange"]
POSITIVE_GROUPS: tuple[Group, ...] = ("pos_sanctioned", "pos_mixer")

_SDN_UID = re.compile(r"\(SDN uid (\d+)\)")


@dataclass(frozen=True)
class FlaggedLabel:
    address: str
    label_type: str
    name: str | None = None

    @property
    def group_key(self) -> str:
        """Addresses of one SDN entity stay on the same side of the split, so an entity's
        other wallets can't leak its held-out address into the scoring labels."""
        if self.label_type == "sanctioned" and self.name:
            match = _SDN_UID.search(self.name)
            if match:
                return f"sdn:{match.group(1)}"
        return f"addr:{self.address}"


def split_labels(
    labels: Sequence[FlaggedLabel], seed: int, heldout_fraction: float = 0.5
) -> tuple[set[str], set[str]]:
    """Return (scoring, heldout) address sets, split by group within each label type."""
    rng = random.Random(seed)
    heldout: set[str] = set()
    for label_type in sorted({label.label_type for label in labels}):
        of_type = [label for label in labels if label.label_type == label_type]
        groups: dict[str, list[str]] = {}
        for label in of_type:
            groups.setdefault(label.group_key, []).append(label.address)
        keys = sorted(groups)
        rng.shuffle(keys)
        target = heldout_fraction * len(of_type)
        taken = 0
        for key in keys:
            if taken >= target:
                break
            heldout.update(groups[key])
            taken += len(groups[key])
    scoring = {label.address for label in labels} - heldout
    return scoring, heldout


@dataclass(frozen=True)
class Example:
    address: str
    label: int  # 1 = positive, 0 = negative
    group: Group
    # How it was chosen: the held-out address it transacted with, or the sampled block.
    source: str


@dataclass
class EvalSet:
    version: str
    created_at: str
    seed: int
    snapshot_block: int
    snapshot_time: str
    # Every label the evaluation depends on, frozen here so a later label ingest (e.g. an
    # OFAC delisting) can't change the results: flagged address -> label type, the split,
    # and the exchange addresses.
    label_sources: dict[str, int]
    flagged_labels: dict[str, str]
    scoring_flagged: list[str]
    heldout_flagged: list[str]
    exchange_labels: list[str]
    # Randomly sampled negatives dropped for transacting directly with a held-out address.
    excluded_contaminated: int = 0
    examples: list[Example] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "EvalSet":
        data = json.loads(text)
        data["examples"] = [Example(**e) for e in data["examples"]]
        return cls(**data)


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def false_positive_rate(self) -> float:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0


def confusion(scores: Sequence[float], labels: Sequence[int], threshold: float) -> Confusion:
    """A wallet is flagged when its score is >= threshold."""
    tp = fp = tn = fn = 0
    for score, label in zip(scores, labels, strict=True):
        flagged = score >= threshold
        if label and flagged:
            tp += 1
        elif label:
            fn += 1
        elif flagged:
            fp += 1
        else:
            tn += 1
    return Confusion(tp, fp, tn, fn)


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion; sound at small n and near 0 or 1."""
    if trials == 0:
        return 0.0, 0.0
    p = successes / trials
    denom = 1 + z**2 / trials
    centre = (p + z**2 / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pr_curve(
    scores: Sequence[float], labels: Sequence[int], thresholds: Iterable[float]
) -> list[tuple[float, float, float]]:
    """(threshold, precision, recall) at each threshold."""
    points = []
    for t in thresholds:
        c = confusion(scores, labels, t)
        points.append((t, c.precision, c.recall))
    return points


def average_precision(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Area under the PR curve as a step function over every distinct score.

    Tied scores are handled as one step, so the result doesn't depend on input order.
    """
    positives = sum(labels)
    if positives == 0:
        return 0.0
    ap = 0.0
    previous_recall = 0.0
    for t in sorted(set(scores), reverse=True):
        c = confusion(scores, labels, t)
        ap += (c.recall - previous_recall) * c.precision
        previous_recall = c.recall
    return ap
