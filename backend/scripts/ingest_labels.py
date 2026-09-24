"""Load curated label seed CSVs (exchange, mixer, malicious) into address_labels.

Each source named in the files is synced: its rows that are no longer in the files are
removed. Columns: address,label_type,source,name,severity (severity optional).

Usage (from backend/): python scripts/ingest_labels.py [config/labels/*.csv ...]
Defaults to every CSV in config/labels/.
"""

import argparse
import asyncio
from pathlib import Path

from app.db.session import get_engine, get_sessionmaker
from app.services.labels import count_labels, parse_seed_csv, sync_labels

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "config" / "labels"


async def main(files: dict[str, str]) -> None:
    records = [r for text in files.values() for r in parse_seed_csv(text)]
    print(f"Files: {', '.join(files)} ({len(records)} labels)")

    async with get_sessionmaker()() as session:
        counts = await sync_labels(session, records)
        totals = await count_labels(session)
    await get_engine().dispose()

    print(
        f"Inserted {counts.inserted}, updated {counts.updated}, "
        f"unchanged {counts.unchanged}, removed {counts.removed}"
    )
    print("Labels in database:")
    for (source, label_type), n in totals.items():
        print(f"  {source:32} {label_type:11} {n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths or sorted(DEFAULT_DIR.glob("*.csv"))
    asyncio.run(main({path.name: path.read_text() for path in paths}))
