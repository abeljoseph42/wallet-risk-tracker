"""Fetch one address's normal transactions through the cache (hits live Etherscan on a miss).

Usage (from backend/): python scripts/fetch_transactions.py 0xADDRESS
Needs ETHERSCAN_API_KEY in the environment or backend/.env.
"""

import argparse
import asyncio
import sys

import httpx

from app.clients.etherscan import EtherscanClient
from app.config import get_settings
from app.core.rate_limiter import TokenBucketRateLimiter
from app.db.session import get_engine, get_sessionmaker
from app.services.cache import TransactionCache


async def main(address: str) -> None:
    settings = get_settings()
    if not settings.etherscan_api_key:
        sys.exit("ETHERSCAN_API_KEY is not set.")

    async with httpx.AsyncClient(timeout=30) as http:
        client = EtherscanClient(
            settings.etherscan_api_key,
            http_client=http,
            rate_limiter=TokenBucketRateLimiter(settings.etherscan_rate_limit_per_sec),
        )
        cache = TransactionCache(get_sessionmaker(), client, ttl_seconds=settings.cache_ttl_seconds)
        result = await cache.get_normal_transactions(address)
    await get_engine().dispose()

    source = "cache hit" if result.cache_hit else f"fetched ({result.upstream_calls} API calls)"
    print(f"{len(result.transactions)} transactions for {address}: {source}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    asyncio.run(main(parser.parse_args().address))
