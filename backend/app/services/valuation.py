"""Convert transfer amounts to ETH-equivalent wei so different assets can be compared."""

from collections.abc import Callable
from decimal import Decimal

from app.config import Valuation

# (token_address or None for ETH, raw amount in the asset's base units) -> ETH-equivalent wei
Valuer = Callable[[str | None, int], int]

_WEI_PER_ETH = Decimal(10**18)


def eth_only(token_address: str | None, value_raw: int) -> int:
    return value_raw if token_address is None else 0


def make_valuer(valuation: Valuation) -> Valuer:
    """ETH at face value; configured stablecoins at $1 via a fixed usd_per_eth; others 0.

    Unknown tokens are worth 0 on purpose: without prices their raw units are meaningless,
    and it keeps spam-token airdrops from creating value-weighted links.
    """
    usd_per_eth = Decimal(str(valuation.usd_per_eth))
    scale = {
        address: _WEI_PER_ETH / (Decimal(10**coin.decimals) * usd_per_eth)
        for address, coin in valuation.stablecoins.items()
    }

    def value(token_address: str | None, value_raw: int) -> int:
        if token_address is None:
            return value_raw
        factor = scale.get(token_address)
        return int(value_raw * factor) if factor is not None else 0

    return value
