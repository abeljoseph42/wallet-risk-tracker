"""Address label ingestion: OFAC SDN (sanctioned) and curated seed files (exchange, mixer, ...).

Each ingest *syncs one source*: rows from that source that are no longer present are
deleted, so an OFAC delisting disappears on the next run. Re-running with unchanged input
changes nothing.
"""

import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import Boolean, ColumnElement, delete, func, literal_column, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.addresses import InvalidAddressError, normalize_address
from app.db.models import LABEL_TYPES, AddressLabel

logger = logging.getLogger(__name__)

# Official Sanctions List Service export (checked 2026-09-24). It 302-redirects to a
# short-lived signed S3 URL, so redirects must be followed.
OFAC_SDN_XML_URL = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
)
OFAC_SOURCE = "ofac_sdn"

_DIGITAL_CURRENCY_PREFIX = "Digital Currency Address - "
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class LabelIngestError(Exception):
    pass


@dataclass(frozen=True)
class LabelRecord:
    address: str
    label_type: str
    source: str
    name: str | None = None
    severity: float | None = None


@dataclass(frozen=True)
class SdnParseResult:
    publish_date: str | None
    labels: list[LabelRecord]


@dataclass(frozen=True)
class SyncCounts:
    inserted: int
    updated: int
    unchanged: int
    removed: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


async def download_sdn_xml(http: httpx.AsyncClient) -> bytes:
    response = await http.get(OFAC_SDN_XML_URL, follow_redirects=True, timeout=120)
    response.raise_for_status()
    if not response.content:
        raise LabelIngestError("OFAC SDN download returned an empty body")
    return response.content


def parse_sdn_xml(xml_bytes: bytes) -> SdnParseResult:
    """Extract every Ethereum-format address listed as a digital currency address.

    Any `Digital Currency Address - <asset>` whose value is a 0x 20-byte address is kept,
    not only `- ETH`: an EVM address is the same key on every EVM chain, and OFAC lists
    some Ethereum addresses only under the token (USDT, USDC) or chain (ARB, BSC) used.
    """
    root = ET.fromstring(xml_bytes)
    publish_date = root.findtext("{*}publshInformation/{*}Publish_Date")

    assets_by_address: dict[str, set[str]] = {}
    name_by_address: dict[str, str] = {}
    for entry in root.iterfind("{*}sdnEntry"):
        uid = entry.findtext("{*}uid", default="")
        name = " ".join(
            part for part in (entry.findtext("{*}firstName"), entry.findtext("{*}lastName")) if part
        )
        for id_el in entry.iterfind("{*}idList/{*}id"):
            id_type = id_el.findtext("{*}idType", default="")
            value = id_el.findtext("{*}idNumber", default="").strip()
            if not id_type.startswith(_DIGITAL_CURRENCY_PREFIX):
                continue
            if not _EVM_ADDRESS_RE.fullmatch(value):
                continue
            address = value.lower()
            assets_by_address.setdefault(address, set()).add(
                id_type.removeprefix(_DIGITAL_CURRENCY_PREFIX)
            )
            name_by_address.setdefault(address, f"{name} (SDN uid {uid})")

    labels = [
        LabelRecord(
            address=address,
            label_type="sanctioned",
            source=OFAC_SOURCE,
            name=f"{name_by_address[address]} [{', '.join(sorted(assets))}]",
        )
        for address, assets in sorted(assets_by_address.items())
    ]
    return SdnParseResult(publish_date=publish_date, labels=labels)


_SEED_COLUMNS = ("address", "label_type", "source", "name", "severity")


def parse_seed_csv(text: str) -> list[LabelRecord]:
    """Parse a curated seed file. Any bad row fails the whole file, naming the line."""
    reader = csv.DictReader(io.StringIO(text))
    missing = {"address", "label_type", "source"} - set(reader.fieldnames or ())
    if missing:
        raise LabelIngestError(f"Seed CSV missing columns: {sorted(missing)}")

    records: list[LabelRecord] = []
    for line, row in enumerate(reader, start=2):
        try:
            address = normalize_address(row["address"])
        except InvalidAddressError as exc:
            raise LabelIngestError(f"line {line}: {exc}") from exc
        label_type = row["label_type"].strip()
        if label_type not in LABEL_TYPES:
            raise LabelIngestError(f"line {line}: unknown label_type {label_type!r}")
        source = row["source"].strip()
        if not source:
            raise LabelIngestError(f"line {line}: empty source")
        severity_raw = (row.get("severity") or "").strip()
        severity = float(severity_raw) if severity_raw else None
        if severity is not None and not 0 <= severity <= 1:
            raise LabelIngestError(f"line {line}: severity must be in [0, 1]")
        records.append(
            LabelRecord(
                address=address,
                label_type=label_type,
                source=source,
                name=(row.get("name") or "").strip() or None,
                severity=severity,
            )
        )
    return records


def format_seed_csv(records: Iterable[LabelRecord]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_SEED_COLUMNS)
    for r in records:
        writer.writerow(
            [
                r.address,
                r.label_type,
                r.source,
                r.name or "",
                "" if r.severity is None else r.severity,
            ]
        )
    return out.getvalue()


async def sync_labels(session: AsyncSession, records: Sequence[LabelRecord]) -> SyncCounts:
    """Make the DB's rows for each source in `records` match `records` exactly.

    Refuses an empty input: a parser that silently finds nothing (e.g. after an upstream
    format change) must not wipe the existing ground truth.
    """
    if not records:
        raise LabelIngestError("No labels parsed; refusing to sync an empty source")

    deduped = {(r.address, r.label_type): r for r in records}
    if len(deduped) != len(records):
        raise LabelIngestError("Duplicate (address, label_type) pairs in input")
    sources = sorted({r.source for r in records})

    base = insert(AddressLabel).values(
        [
            {
                "address": r.address,
                "label_type": r.label_type,
                "source": r.source,
                "name": r.name,
                "severity": r.severity,
            }
            for r in records
        ]
    )
    excluded = base.excluded
    # Update only rows whose content actually changed, so an unchanged re-run reports 0.
    stmt = base.on_conflict_do_update(
        index_elements=["address", "label_type"],
        set_={"source": excluded.source, "name": excluded.name, "severity": excluded.severity},
        where=or_(
            AddressLabel.source.is_distinct_from(excluded.source),
            AddressLabel.name.is_distinct_from(excluded.name),
            AddressLabel.severity.is_distinct_from(excluded.severity),
        ),
    ).returning(AddressLabel.address, _inserted_flag())
    written = (await session.execute(stmt)).all()
    inserted = sum(1 for row in written if row[1])
    updated = len(written) - inserted

    keep = list(deduped)
    removed_rows = await session.execute(
        delete(AddressLabel)
        .where(
            AddressLabel.source.in_(sources),
            tuple_(AddressLabel.address, AddressLabel.label_type).not_in(keep),
        )
        .returning(AddressLabel.address)
    )
    removed = len(removed_rows.all())
    await session.commit()

    counts = SyncCounts(
        inserted=inserted,
        updated=updated,
        unchanged=len(records) - inserted - updated,
        removed=removed,
    )
    logger.info("Synced labels for %s: %s", ", ".join(sources), counts)
    return counts


def _inserted_flag() -> ColumnElement[bool]:
    # Postgres sets xmax = 0 on a freshly inserted row version; on the update path of an
    # upsert it is non-zero. This is the standard way to tell the two apart in RETURNING.
    return literal_column("xmax = 0", type_=Boolean)


async def count_labels(session: AsyncSession) -> dict[tuple[str, str], int]:
    rows = await session.execute(
        select(AddressLabel.source, AddressLabel.label_type, func.count())
        .group_by(AddressLabel.source, AddressLabel.label_type)
        .order_by(AddressLabel.source, AddressLabel.label_type)
    )
    return {(source, label_type): n for source, label_type, n in rows.all()}


async def load_label_index(session: AsyncSession) -> dict[str, frozenset[str]]:
    """Every labeled address and its label types. Small enough (hundreds) to hold in memory."""
    rows = await session.execute(select(AddressLabel.address, AddressLabel.label_type))
    index: dict[str, set[str]] = {}
    for address, label_type in rows.all():
        index.setdefault(address, set()).add(label_type)
    return {address: frozenset(types) for address, types in index.items()}
