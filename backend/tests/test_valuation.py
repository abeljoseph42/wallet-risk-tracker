"""Tests for ETH-equivalent valuation of transfers."""

from app.config import Stablecoin, Valuation
from app.services.valuation import eth_only, make_valuer

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
SPAM = "0x" + "5" * 40

VALUATION = Valuation(
    usd_per_eth=2000,
    stablecoins={
        USDC.upper().replace("0X", "0x"): Stablecoin(symbol="USDC", decimals=6),
        DAI: Stablecoin(symbol="DAI", decimals=18),
    },
)


def test_eth_is_face_value() -> None:
    assert make_valuer(VALUATION)(None, 123) == 123


def test_stablecoins_convert_at_one_dollar_and_fixed_eth_price() -> None:
    value = make_valuer(VALUATION)

    # $2,000 of USDC (6 decimals) at $2,000/ETH = 1 ETH.
    assert value(USDC, 2_000 * 10**6) == 10**18
    # $1,000 of DAI (18 decimals) = 0.5 ETH.
    assert value(DAI, 1_000 * 10**18) == 5 * 10**17


def test_unknown_tokens_are_worth_nothing() -> None:
    assert make_valuer(VALUATION)(SPAM, 10**30) == 0


def test_stablecoin_addresses_are_matched_case_insensitively() -> None:
    assert USDC in VALUATION.stablecoins


def test_eth_only_ignores_all_tokens() -> None:
    assert eth_only(None, 5) == 5
    assert eth_only(USDC, 5) == 0
