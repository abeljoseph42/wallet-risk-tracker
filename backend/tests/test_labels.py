"""Tests for label parsing (OFAC SDN XML, seed CSV) and source-scoped sync."""

import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AddressLabel
from app.services.labels import (
    OFAC_SOURCE,
    LabelIngestError,
    LabelRecord,
    SyncCounts,
    count_labels,
    format_seed_csv,
    load_severity_overrides,
    parse_sdn_xml,
    parse_seed_csv,
    sync_labels,
)

FIXTURES = Path(__file__).parent / "fixtures"
MIXER = "0xabcdef0000000000000000000000000000000002"


def _sdn() -> bytes:
    return (FIXTURES / "sdn_sample.xml").read_bytes()


def _seed() -> str:
    return (FIXTURES / "labels_seed_sample.csv").read_text()


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[AddressLabel]:
    async with sessions() as session:
        result = await session.scalars(
            select(AddressLabel).order_by(AddressLabel.address, AddressLabel.label_type)
        )
        return list(result)


def test_parse_sdn_keeps_evm_addresses_from_any_digital_currency_type() -> None:
    result = parse_sdn_xml(_sdn())

    assert result.publish_date == "09/23/2026"
    assert [label.address for label in result.labels] == [
        "0x1111111111111111111111111111111111111111",
        "0x3333333333333333333333333333333333333333",
        MIXER,
    ]
    assert {label.label_type for label in result.labels} == {"sanctioned"}
    assert {label.source for label in result.labels} == {OFAC_SOURCE}


def test_parse_sdn_merges_assets_for_one_address_and_names_the_entry() -> None:
    labels = {label.address: label for label in parse_sdn_xml(_sdn()).labels}

    assert labels[MIXER].name == "EXAMPLE MIXER LTD (SDN uid 1002) [ETH, USDT]"
    assert labels["0x1111111111111111111111111111111111111111"].name == (
        "Jane DOE (SDN uid 1001) [ETH]"
    )


def test_parse_sdn_with_no_digital_currency_addresses_returns_nothing() -> None:
    xml = (
        b'<sdnList xmlns="urn:x"><sdnEntry><uid>1</uid><lastName>X</lastName></sdnEntry></sdnList>'
    )

    assert parse_sdn_xml(xml).labels == []


def test_parse_seed_normalizes_addresses_and_reads_optional_severity() -> None:
    records = parse_seed_csv(_seed())

    assert [r.address for r in records][-1] == MIXER
    assert records[0].severity is None
    assert records[-1].severity == 0.9
    assert records[0].name == "Example Exchange 1"


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ("0x123,exchange,s,,", "line 2: Not a valid Ethereum address"),
        ("0x" + "4" * 40 + ",casino,s,,", "line 2: unknown label_type 'casino'"),
        ("0x" + "4" * 40 + ",exchange,,,", "line 2: empty source"),
        ("0x" + "4" * 40 + ",exchange,s,,1.5", "line 2: severity must be in"),
    ],
)
def test_parse_seed_rejects_bad_rows_with_line_numbers(row: str, error: str) -> None:
    with pytest.raises(LabelIngestError, match=error):
        parse_seed_csv(f"address,label_type,source,name,severity\n{row}\n")


def test_seed_csv_round_trips_through_format() -> None:
    records = parse_seed_csv(_seed())

    assert parse_seed_csv(format_seed_csv(records)) == records


async def test_sync_inserts_then_is_idempotent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    labels = parse_sdn_xml(_sdn()).labels

    async with sessions() as session:
        first = await sync_labels(session, labels)
    async with sessions() as session:
        second = await sync_labels(session, labels)

    assert first == SyncCounts(inserted=3, updated=0, unchanged=0, removed=0)
    assert second == SyncCounts(inserted=0, updated=0, unchanged=3, removed=0)
    assert len(await _rows(sessions)) == 3


async def test_sync_removes_delisted_addresses_and_updates_changed_ones(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    labels = parse_sdn_xml(_sdn()).labels
    async with sessions() as session:
        await sync_labels(session, labels)

    delisted, kept, renamed = labels
    new_list = [kept, dataclasses.replace(renamed, name="RENAMED LTD")]
    async with sessions() as session:
        counts = await sync_labels(session, new_list)

    assert counts == SyncCounts(inserted=0, updated=1, unchanged=1, removed=1)
    rows = await _rows(sessions)
    assert delisted.address not in {r.address for r in rows}
    assert {r.name for r in rows if r.address == renamed.address} == {"RENAMED LTD"}


async def test_sync_leaves_other_sources_alone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        await sync_labels(session, parse_sdn_xml(_sdn()).labels)
        await sync_labels(session, parse_seed_csv(_seed()))
        counts = await count_labels(session)

    assert counts == {
        (OFAC_SOURCE, "sanctioned"): 3,
        ("test-seed", "exchange"): 2,
        ("test-seed", "mixer"): 1,
    }
    # The mixer address is also sanctioned: one address, two labels.
    assert {r.label_type for r in await _rows(sessions) if r.address == MIXER} == {
        "mixer",
        "sanctioned",
    }


async def test_sync_refuses_empty_input_instead_of_wiping(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        await sync_labels(session, parse_sdn_xml(_sdn()).labels)
        with pytest.raises(LabelIngestError, match="refusing"):
            await sync_labels(session, [])

    assert len(await _rows(sessions)) == 3


async def test_sync_rejects_duplicate_pairs(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    record = LabelRecord(address=MIXER, label_type="mixer", source="s")

    async with sessions() as session:
        with pytest.raises(LabelIngestError, match="Duplicate"):
            await sync_labels(session, [record, record])


async def test_severity_overrides_load_only_rows_that_set_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        await sync_labels(session, parse_seed_csv(_seed()))
        overrides = await load_severity_overrides(session)

    assert overrides == {(MIXER, "mixer"): 0.9}
