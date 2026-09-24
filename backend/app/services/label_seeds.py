"""Curation rules for building the exchange/mixer seed from the etherscan-labels dump.

Source: github.com/brianleect/etherscan-labels (MIT), a scrape of Etherscan name tags,
pinned to a commit so the seed is reproducible. Only an exchange's operational wallets
count as `exchange` (hot/cold/numbered wallets, deposit funders, old addresses); its token
contracts, deployers, commerce/fee addresses and DEX routers do not. Only Tornado Cash
deposit pools, mixer, proxy and router contracts count as `mixer`; governance, vesting,
token and donation contracts do not.
"""

import csv
import io
import re
from collections.abc import Iterable

from app.core.addresses import InvalidAddressError, normalize_address
from app.services.labels import LabelRecord

ETHERSCAN_LABELS_COMMIT = "923aba72c7e2d0682f7ae6194b6140bd90668dc9"
ETHERSCAN_LABELS_SOURCE = f"etherscan-labels@{ETHERSCAN_LABELS_COMMIT[:7]}"
ETHERSCAN_LABELS_URL = (
    "https://raw.githubusercontent.com/brianleect/etherscan-labels/"
    f"{ETHERSCAN_LABELS_COMMIT}/data/etherscan/accounts/{{file}}.csv"
)

EXCHANGE_FILES = (
    "binance", "bitfinex", "bithumb", "bitmart", "bitstamp", "bittrex", "coinbase",
    "crypto-com", "ftx", "gate-io", "gemini", "hitbtc", "huobi", "kraken", "kucoin",
    "okx", "poloniex", "upbit",
)  # fmt: skip
MIXER_FILES = ("tornado-cash", "ethereum-mixer")

_NOT_AN_EXCHANGE_WALLET = re.compile(
    r"\b(tokens?|deployer|contract|commerce|dex|blacklister|controller|cdcnft|nft|unlock|"
    r"vesting|multisig|proxy|router|staking|bridge|3x)\b",
    re.IGNORECASE,
)
_TORNADO_MIXER = re.compile(
    r"^Tornado\.Cash: (Mixer \d+|Proxy|Old Proxy|Router|[\d,.]+ \w+( \d+)?)$"
)


def is_exchange_wallet(name: str) -> bool:
    return bool(name.strip()) and not _NOT_AN_EXCHANGE_WALLET.search(name)


def is_mixer_contract(name: str) -> bool:
    return bool(_TORNADO_MIXER.fullmatch(name.strip()))


def select_labels(file_name: str, csv_text: str) -> list[LabelRecord]:
    """Apply the curation rule for `file_name` to one etherscan-labels CSV."""
    if file_name in EXCHANGE_FILES:
        label_type, keep = "exchange", is_exchange_wallet
    elif file_name in MIXER_FILES:
        label_type, keep = "mixer", is_mixer_contract
    else:
        raise ValueError(f"No curation rule for {file_name!r}")

    records = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        name = (row.get("Name Tag") or "").strip()
        if not keep(name):
            continue
        try:
            address = normalize_address(row.get("Address") or "")
        except InvalidAddressError:
            continue
        records.append(
            LabelRecord(
                address=address, label_type=label_type, source=ETHERSCAN_LABELS_SOURCE, name=name
            )
        )
    return records


def merge_labels(groups: Iterable[list[LabelRecord]]) -> list[LabelRecord]:
    """De-duplicate by (address, label_type), keeping the first; sort for stable diffs."""
    merged: dict[tuple[str, str], LabelRecord] = {}
    for group in groups:
        for record in group:
            merged.setdefault((record.address, record.label_type), record)
    return sorted(merged.values(), key=lambda r: (r.label_type, r.name or "", r.address))
