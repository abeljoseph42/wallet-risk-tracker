"""Tests for the exchange/mixer curation rules applied to the etherscan-labels dump."""

import pytest

from app.services.label_seeds import (
    ETHERSCAN_LABELS_SOURCE,
    is_exchange_wallet,
    is_mixer_contract,
    merge_labels,
    select_labels,
)


@pytest.mark.parametrize(
    "name",
    [
        "Binance",
        "Binance 14",
        "Coinbase 10",
        "Kraken: Hot Wallet",
        "Upbit: Cold Wallet",
        "OKX: Deposit Funder 3",
        "Bithumb: Old Address 2",
        "Poloniex: BAT",
    ],
)
def test_exchange_operational_wallets_are_kept(name: str) -> None:
    assert is_exchange_wallet(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Binance: BNB Token",
        "Binance: Binance-Peg Tokens",
        "Kraken: Deployer 1",
        "Coinbase: Commerce Fee 2",
        "Coinbase: Account Blacklister",
        "OKX DEX: Aggregation Router",
        "Crypto.com: CDCNFT Token",
        "FTX: 3X Long Ethereum Token",
        "KuCoin Contract",
    ],
)
def test_exchange_non_wallet_contracts_are_dropped(name: str) -> None:
    assert not is_exchange_wallet(name)


@pytest.mark.parametrize(
    "name",
    [
        "Tornado.Cash: 0.1 ETH",
        "Tornado.Cash: 100,000 DAI",
        "Tornado.Cash: 1,00 USDC",
        "Tornado.Cash: 500,000 cDAI 2",
        "Tornado.Cash: Mixer 1",
        "Tornado.Cash: Proxy",
        "Tornado.Cash: Old Proxy",
        "Tornado.Cash: Router",
    ],
)
def test_tornado_pools_and_entry_contracts_are_mixers(name: str) -> None:
    assert is_mixer_contract(name)


@pytest.mark.parametrize(
    "name",
    [
        "Gitcoin Grants: Tornado.cash",
        "Tornado.Cash: Governance",
        "Tornado.Cash: Team 1 Vesting",
        "Tornado.Cash: TORN Token",
        "Tornado.Cash: Donate",
        "Tornado.Cash: Relayer Registry",
    ],
)
def test_tornado_governance_and_tokens_are_not_mixers(name: str) -> None:
    assert not is_mixer_contract(name)


_CSV = """,Address,Name Tag,Balance,Txn Count
0,0x28C6c06298d514Db089934071355E5743bf21d60,Binance 14,1 ETH,100
1,0xB8c77482e45F1F44dE1745F52C74426C631bDD52,Binance: BNB Token,0 ETH,5
2,not-an-address,Binance 99,0 ETH,1
"""


def test_select_labels_applies_rule_and_normalizes() -> None:
    records = select_labels("binance", _CSV)

    assert len(records) == 1
    assert records[0].address == "0x28c6c06298d514db089934071355e5743bf21d60"
    assert records[0].label_type == "exchange"
    assert records[0].source == ETHERSCAN_LABELS_SOURCE
    assert records[0].name == "Binance 14"


def test_select_labels_rejects_files_without_a_rule() -> None:
    with pytest.raises(ValueError, match="No curation rule"):
        select_labels("opensea", _CSV)


def test_merge_labels_dedupes_across_files() -> None:
    mixer_csv = (
        ",Address,Name Tag,Balance,Txn Count\n0,0x" + "9" * 40 + ",Tornado.Cash: Mixer 1,0,1\n"
    )

    merged = merge_labels(
        [select_labels("tornado-cash", mixer_csv), select_labels("ethereum-mixer", mixer_csv)]
    )

    assert len(merged) == 1
