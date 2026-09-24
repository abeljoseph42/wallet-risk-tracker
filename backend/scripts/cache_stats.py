"""Print cache hit rate and Etherscan calls saved, measured from `api_metrics`.

Usage (from backend/): python scripts/cache_stats.py
"""

import asyncio

from app.db.session import get_engine, get_sessionmaker
from app.services.cache import summarize_metrics


async def main() -> None:
    async with get_sessionmaker()() as session:
        m = await summarize_metrics(session)
    await get_engine().dispose()

    print(f"Lookups:                 {m.lookups}")
    print(f"Cache hits:              {m.hits}")
    print(f"Cache hit rate:          {m.hit_rate:.1%}")
    print(f"Etherscan calls made:    {m.upstream_calls}")
    print(f"Etherscan calls saved:   {m.calls_saved}  (lower bound: txlist pages per hit)")
    print(f"Call reduction:          {m.call_reduction:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
