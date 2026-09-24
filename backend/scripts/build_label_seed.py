"""Regenerate config/labels/etherscan_labels.csv from the pinned etherscan-labels commit.

The output is committed; rerun only to change the curation rules or the pinned commit.
Usage (from backend/): python scripts/build_label_seed.py
"""

import asyncio
from collections import Counter
from pathlib import Path

import httpx

from app.services.label_seeds import (
    ETHERSCAN_LABELS_URL,
    EXCHANGE_FILES,
    MIXER_FILES,
    merge_labels,
    select_labels,
)
from app.services.labels import LabelRecord, format_seed_csv

OUTPUT = Path(__file__).resolve().parent.parent / "config" / "labels" / "etherscan_labels.csv"


async def fetch_labels() -> list[LabelRecord]:
    groups = []
    async with httpx.AsyncClient(timeout=60) as http:
        for file_name in (*EXCHANGE_FILES, *MIXER_FILES):
            response = await http.get(ETHERSCAN_LABELS_URL.format(file=file_name))
            response.raise_for_status()
            groups.append(select_labels(file_name, response.text))

    return merge_labels(groups)


if __name__ == "__main__":
    records = asyncio.run(fetch_labels())
    OUTPUT.write_text(format_seed_csv(records))
    counts = Counter(r.label_type for r in records)
    print(f"Wrote {len(records)} labels to {OUTPUT.name}: {dict(sorted(counts.items()))}")
